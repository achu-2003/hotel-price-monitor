#!/usr/bin/env python
"""Carry the RMS room mappings from one deployment to another, by name.

WHY THIS EXISTS
===============
The mapping is the translation between what the booking site calls a room and
what the rate application calls it, and it is typed in by hand on the Repricing
page. That is fine once. Standing up a second deployment means typing it again,
and a deployment that is mapped differently from the one it was tested on is
the hardest kind of wrong: every page renders, every run succeeds, and the
rates land on the wrong rooms.

It is also easy to get subtly wrong by hand, because the pairing is not
guessable. ASG's "Standard Double Room" is RMS's DELUXE and its "Deluxe Double
Room" is RMS's CLASSIC -- the crossover is real, and reading it off a screen
and retyping it is exactly the operation that swaps two rows.

NAMES, NEVER IDS
================
``rms_room_mappings`` keys on ``room_type_id``, and room type ids are per
database: room 14 on the server is not room 14 on the laptop. Copying the
table -- a dump, a CSV, a SELECT piped into an INSERT -- therefore points each
mapping at whatever room happens to hold that id on the other side, silently.

So this exports the hotel name and the room name, and resolves them again on
the way in. A name that does not resolve is REPORTED AND SKIPPED rather than
guessed at: a mapping written against the wrong room is worse than one that is
missing, because the missing one announces itself on the page as "not mapped"
and the wrong one does not announce itself at all.

USE
===
On the machine that is already mapped::

    .venv312\\Scripts\\python.exe scripts/copy_room_mappings.py --export mappings.json

Copy the file across, then on the machine that needs them::

    .venv312\\Scripts\\python.exe scripts/copy_room_mappings.py --import mappings.json
    .venv312\\Scripts\\python.exe scripts/copy_room_mappings.py --import mappings.json --yes

It shows what it would write and needs ``--yes`` to write it. Importing twice
is safe: a room that already has a mapping is UPDATED to match the file, which
is what makes this usable to correct a deployment as well as to seed one.

Run it as the same owner on both sides, or pass ``--owner`` to say who the
mappings belong to here. Mappings are per owner, and the import will not
invent an account.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

# Running `python scripts/copy_room_mappings.py` puts scripts/ on sys.path, not
# the project root, so `import app` would fail. Same fix as create_account.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import sync_session  # noqa: E402

#: Everything that makes a mapping what it is. ``is_enabled`` is carried
#: because a room deliberately left out of repricing is a decision, and a copy
#: that silently switched it back on would start moving a rate nobody meant to
#: move.
FIELDS = (
    "rms_room",
    "rate_type_ep",
    "rate_type_cp",
    "rate_type_map",
    "floor_amount",
    "ceiling_amount",
    "is_enabled",
)


#: The rule's own numbers. Carried by name where they point at a row:
#: ``benchmark_hotel_id`` and the keys of ``benchmark_room_pairs`` are ids,
#: and ids are per database exactly as room_type_id is.
#:
#: ``auto_enabled`` IS DELIBERATELY NOT HERE. Copying a file must never be
#: what starts a machine moving live rates by itself; that switch has its own
#: endpoint and its own audit line on purpose, and it should stay a thing
#: somebody turns on while looking at the page it affects.
SETTINGS_FIELDS = (
    "position_pct",
    "max_step_pct",
    "floor_pct",
    "ceiling_pct",
    "min_competitors",
    "round_to",
    "channel",
    "weekend_pct",
    "sold_out_pct",
    "benchmark_undercut",
    "benchmark_with_tax",
    "benchmark_meal_plan",
)


def _owner(session, username: str | None) -> tuple[int, str]:
    """The account the mappings belong to.

    Named rather than assumed when there is more than one, because writing
    another owner's mappings would be invisible here and wrong on their page.
    """
    if username:
        row = session.execute(
            text("select id, username from users where username = :u"), {"u": username}
        ).mappings().first()
        if row is None:
            raise SystemExit(f"No user {username!r}.")
        return row["id"], row["username"]

    rows = session.execute(
        text("select id, username from users order by id")
    ).mappings().all()
    if len(rows) == 1:
        return rows[0]["id"], rows[0]["username"]
    raise SystemExit(
        "More than one account here; say which with --owner. Known: "
        + ", ".join(r["username"] for r in rows)
    )


def _settings_out(session, owner_id: int) -> dict | None:
    """The rule, with every id turned back into the name it stands for."""
    row = session.execute(
        text(
            "select " + ", ".join(SETTINGS_FIELDS) + ", benchmark_hotel_id, "
            "       benchmark_room_pairs "
            "from repricing_settings where owner_user_id = :o"
        ),
        {"o": owner_id},
    ).mappings().first()
    if row is None:
        return None

    out = {f: (str(row[f]) if isinstance(row[f], Decimal) else row[f]) for f in SETTINGS_FIELDS}
    out["benchmark_hotel"] = session.execute(
        text("select name from hotels where id = :h"), {"h": row["benchmark_hotel_id"]}
    ).scalar() if row["benchmark_hotel_id"] else None

    # {our room id: their room name} becomes {our room NAME: their room name}.
    # Only our side is an id; theirs is already a name, because the rule
    # re-matches it on the page every night.
    pairs = {}
    for room_id, their_room in (row["benchmark_room_pairs"] or {}).items():
        name = session.execute(
            text("select name from room_types where id = :r"), {"r": int(room_id)}
        ).scalar()
        if name:
            pairs[name] = their_room
    out["benchmark_room_pairs"] = pairs
    return out


def _settings_in(session, owner_id: int, wanted: dict) -> tuple[dict, list[str]]:
    """Turn the names back into this database's ids. Unresolved names are named."""
    params = {f: wanted.get(f) for f in SETTINGS_FIELDS}
    unresolved: list[str] = []

    hotel_name = wanted.get("benchmark_hotel")
    params["benchmark_hotel_id"] = None
    if hotel_name:
        # The same three conditions the API's _own_competitor applies, so a
        # benchmark written here is one the endpoint would have accepted.
        hid = session.execute(
            text(
                "select id from hotels where name = :n and owner_user_id = :o "
                "and is_active and not is_own_property"
            ),
            {"n": hotel_name, "o": owner_id},
        ).scalar()
        if hid is None:
            unresolved.append(f"benchmark hotel {hotel_name!r} (not an active competitor here)")
        params["benchmark_hotel_id"] = hid

    pairs = {}
    for our_room, their_room in (wanted.get("benchmark_room_pairs") or {}).items():
        rid = session.execute(
            text(
                "select rt.id from room_types rt join hotels h on h.id = rt.hotel_id "
                "where rt.name = :r and h.owner_user_id = :o and h.is_own_property"
            ),
            {"r": our_room, "o": owner_id},
        ).scalar()
        if rid is None:
            unresolved.append(f"room pair {our_room!r}")
            continue
        pairs[str(rid)] = their_room
    params["benchmark_room_pairs"] = json.dumps(pairs)
    return params, unresolved


def export(path: Path, username: str | None) -> int:
    with sync_session() as s:
        owner_id, owner_name = _owner(s, username)
        rows = s.execute(
            text(
                "select h.name as hotel, rt.name as room, "
                "       m.rms_room, m.rate_type_ep, m.rate_type_cp, m.rate_type_map, "
                "       m.floor_amount, m.ceiling_amount, m.is_enabled "
                "from rms_room_mappings m "
                "join room_types rt on rt.id = m.room_type_id "
                "join hotels h on h.id = rt.hotel_id "
                "where m.owner_user_id = :o "
                "order by h.name, rt.sort_order, rt.name"
            ),
            {"o": owner_id},
        ).mappings().all()
        settings = _settings_out(s, owner_id)

    if not rows:
        print(f"{owner_name} has no room mappings here. Nothing to export.", file=sys.stderr)
        return 1

    payload = {
        "owner": owner_name,
        "settings": settings,
        "mappings": [
            {
                "hotel": r["hotel"],
                "room": r["room"],
                # Decimal is not JSON, and float would round a floor. Strings
                # go back through Decimal unchanged on the way in.
                **{f: (str(r[f]) if isinstance(r[f], Decimal) else r[f]) for f in FIELDS},
            }
            for r in rows
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    for m in payload["mappings"]:
        plans = " ".join(
            f"{p.upper()}={m['rate_type_' + p]}" for p in ("ep", "cp", "map") if m["rate_type_" + p]
        )
        print(f"  {m['hotel']} / {m['room']} -> {m['rms_room']}  {plans}")
    if settings:
        print(f"\n  rule: under {settings['benchmark_hotel'] or 'the median'} "
              f"by {settings['benchmark_undercut']}"
              f"{', tax in' if settings['benchmark_with_tax'] else ''}"
              f"{', on ' + settings['benchmark_meal_plan'] if settings['benchmark_meal_plan'] else ''}"
              f"; step {settings['max_step_pct']}%, floor {settings['floor_pct']}%, "
              f"ceiling {settings['ceiling_pct']}%")
        for ours, theirs in (settings["benchmark_room_pairs"] or {}).items():
            print(f"        {ours} competes with their {theirs}")

    print(f"\n{len(rows)} mapping(s) for {owner_name} written to {path}.")
    print("Import with --import; add --settings to carry the rule across too.")
    return 0


def import_(path: Path, username: str | None, write: bool, with_settings: bool) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    wanted = payload.get("mappings") or []
    if not wanted:
        print(f"{path} holds no mappings.", file=sys.stderr)
        return 2

    with sync_session() as s:
        owner_id, owner_name = _owner(s, username)
        if payload.get("owner") and payload["owner"] != owner_name:
            print(f"  note: exported by {payload['owner']}, importing as {owner_name}")

        planned, missing = [], []
        for m in wanted:
            # Resolved against THIS database, every time. The room is looked up
            # inside its own hotel rather than by name alone: two properties of
            # one owner may both have a "Deluxe Double Room".
            room = s.execute(
                text(
                    "select rt.id, rt.name, h.name as hotel "
                    "from room_types rt join hotels h on h.id = rt.hotel_id "
                    "where h.name = :h and rt.name = :r and h.owner_user_id = :o"
                ),
                {"h": m["hotel"], "r": m["room"], "o": owner_id},
            ).mappings().first()
            if room is None:
                missing.append(f"{m['hotel']} / {m['room']}")
                continue
            existing = s.execute(
                text("select rms_room from rms_room_mappings where room_type_id = :r"),
                {"r": room["id"]},
            ).scalar()
            planned.append((room, m, existing))

        for room, m, existing in planned:
            plans = " ".join(
                f"{p.upper()}={m['rate_type_' + p]}"
                for p in ("ep", "cp", "map")
                if m["rate_type_" + p]
            )
            was = f"  (was {existing})" if existing else ""
            verb = "update" if existing else "create"
            print(f"  {verb:6} {room['hotel']} / {room['name']} -> {m['rms_room']}  {plans}{was}")

        rule_params, rule_unresolved = None, []
        if with_settings:
            if not payload.get("settings"):
                print("\n  --settings asked for, but the file carries none.", file=sys.stderr)
                return 2
            rule_params, rule_unresolved = _settings_in(s, owner_id, payload["settings"])
            w = payload["settings"]
            print(f"\n  rule:  under {w.get('benchmark_hotel') or 'the median'} "
                  f"by {w.get('benchmark_undercut')}"
                  f"{', on ' + w['benchmark_meal_plan'] if w.get('benchmark_meal_plan') else ''}"
                  f"; step {w.get('max_step_pct')}%, floor {w.get('floor_pct')}%, "
                  f"ceiling {w.get('ceiling_pct')}%")
            for ours, theirs in (w.get("benchmark_room_pairs") or {}).items():
                print(f"         {ours} competes with their {theirs}")
            print("         (the automatic switch is never copied; turn it on "
                  "from the page)")
            missing.extend(rule_unresolved)

        if missing:
            # Loudly, and without writing the rest by default: a partial copy
            # that nobody noticed is a deployment mapped differently from the
            # one it was tested on, which is the failure this script exists to
            # prevent.
            print("\nNO ROOM OF THAT NAME HERE:", file=sys.stderr)
            for name in missing:
                print(f"  {name}", file=sys.stderr)
            print(
                "\nRename the room to match, or edit the file. Nothing was written.",
                file=sys.stderr,
            )
            return 2

        if not write:
            extra = " and the rule" if rule_params else ""
            print(f"\nDry run. Re-run with --yes to write {len(planned)} mapping(s){extra}.")
            return 0

        for room, m, _ in planned:
            s.execute(
                text(
                    "insert into rms_room_mappings "
                    "  (owner_user_id, room_type_id, rms_room, rate_type_ep, rate_type_cp, "
                    "   rate_type_map, floor_amount, ceiling_amount, is_enabled) "
                    # created_at/updated_at carry server-side defaults, which
                    # base.py keeps precisely so a script's insert is stamped
                    # like any other. The conflict path sets updated_at itself:
                    # nothing defaults on an UPDATE.
                    "values (:o, :r, :room, :ep, :cp, :map, :floor, :ceil, :on) "
                    # room_type_id is unique, which is what makes a second run
                    # a correction rather than a duplicate.
                    "on conflict (room_type_id) do update set "
                    "  rms_room = excluded.rms_room, "
                    "  rate_type_ep = excluded.rate_type_ep, "
                    "  rate_type_cp = excluded.rate_type_cp, "
                    "  rate_type_map = excluded.rate_type_map, "
                    "  floor_amount = excluded.floor_amount, "
                    "  ceiling_amount = excluded.ceiling_amount, "
                    "  is_enabled = excluded.is_enabled, "
                    "  updated_at = now()"
                ),
                {
                    "o": owner_id,
                    "r": room["id"],
                    "room": m["rms_room"],
                    "ep": m["rate_type_ep"],
                    "cp": m["rate_type_cp"],
                    "map": m["rate_type_map"],
                    "floor": Decimal(m["floor_amount"]) if m["floor_amount"] is not None else None,
                    "ceil": Decimal(m["ceiling_amount"]) if m["ceiling_amount"] is not None else None,
                    "on": m["is_enabled"],
                },
            )

        if rule_params is not None:
            rule_params["o"] = owner_id
            s.execute(
                text(
                    "update repricing_settings set "
                    + ", ".join(f"{f} = :{f}" for f in SETTINGS_FIELDS)
                    + ", benchmark_hotel_id = :benchmark_hotel_id"
                    # Cast explicitly. The parameter arrives as JSON text
                    # and the column is jsonb, which Postgres will not
                    # coerce on its own inside an UPDATE.
                    + ", benchmark_room_pairs = cast(:benchmark_room_pairs as jsonb)"
                    # No default fires on an UPDATE, unlike the insert above.
                    + ", updated_at = now() where owner_user_id = :o"
                ),
                rule_params,
            )
        s.commit()

    print(f"\nWrote {len(planned)} mapping(s) for {owner_name}"
          f"{' and the rule' if with_settings else ''}.")
    print("Open the Repricing page: every room should now name its RMS row, and "
          "Preview in RMS has something to read.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--export", metavar="FILE", help="write this machine's mappings out")
    what.add_argument("--import", dest="import_", metavar="FILE", help="read them in")
    ap.add_argument("--owner", help="the username the mappings belong to")
    ap.add_argument("--settings", action="store_true",
                    help="carry the rule as well: the benchmark hotel, the gap, "
                         "the board, the room pairs and the limits (import only)")
    ap.add_argument("--yes", action="store_true", help="actually write them (import only)")
    args = ap.parse_args()

    if args.export:
        return export(Path(args.export), args.owner)
    return import_(Path(args.import_), args.owner, args.yes, args.settings)


if __name__ == "__main__":
    raise SystemExit(main())
