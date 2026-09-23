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

    if not rows:
        print(f"{owner_name} has no room mappings here. Nothing to export.", file=sys.stderr)
        return 1

    payload = {
        "owner": owner_name,
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
    print(f"\n{len(rows)} mapping(s) for {owner_name} written to {path}.")
    return 0


def import_(path: Path, username: str | None, write: bool) -> int:
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
            print(f"\nDry run. Re-run with --yes to write {len(planned)} mapping(s).")
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
        s.commit()

    print(f"\nWrote {len(planned)} mapping(s) for {owner_name}.")
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
    ap.add_argument("--yes", action="store_true", help="actually write them (import only)")
    args = ap.parse_args()

    if args.export:
        return export(Path(args.export), args.owner)
    return import_(Path(args.import_), args.owner, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
