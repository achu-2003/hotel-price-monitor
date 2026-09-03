#!/usr/bin/env python
"""Re-derive a source's URL template and selectors, on purpose.

WHY THIS EXISTS
===============
Self-repair only runs when a fetch FAILS. That covers a source whose selectors
have stopped matching, and it is blind to the failure that matters more: a
config that succeeds while being wrong.

Both have happened here. One hotel was monitored for two days against the
similar-hotels carousel, reporting four neighbouring properties as its rooms.
Another reported rooms called "Bed:", "Bedroom:" and "Beds:". A third asked
for the same night forever, because its URL carried both dates in one opaque
parameter that could not be templated. Every one of those fetches succeeded,
so no repair was ever triggered, and nothing in the application exposes a way
to ask for one.

WHAT IT DOES
============
Two things, in order, because the second depends on the first:

1. Re-runs ``parameterise_url`` over the stored URL. A URL stored before the
   templating understood its date format is rewritten now, so discovery
   inspects the night being monitored rather than the night someone pasted.

2. Runs the ordinary repair task. Not a private code path: the same
   ``rediscover_source`` the workers call, with the same verification bar, the
   same merge, the same audit trail. A result that does not verify is declined
   here exactly as it would be there.

USAGE
-----
    python scripts/rediscover.py --list
    python scripts/rediscover.py ananthyam
    python scripts/rediscover.py treebo --dry-run
    python scripts/rediscover.py --all --check-in 2026-09-10
    python scripts/rediscover.py midvalley --retire-unseen
    python scripts/rediscover.py midvalley --retire-unseen --force

--force ignores the repair cooldown. A source repaired in the last six
hours is refused otherwise, which is right for an automatic repair and
wrong for a person who has just fixed the scanner and wants to apply it.
It forces an ATTEMPT, never a result: the verification bar afterwards is
unchanged, and only the repair bookkeeping is cleared -- never selectors.

--retire-unseen also DELETES the rooms the repaired config no longer reads.
A config that invented rooms leaves them behind, and they do not sit quietly:
the next fetch cannot find them, so each is recorded as having sold out and
reported to whoever is on the recipient list. One property showed four
competitor hotels on its dashboard as its own sold-out rooms.

It is an assertion, not an instruction. The names are handed to the repair as
candidates and ``names_to_retire`` keeps only those the repaired config does
not read back from the page, so a room that is really there survives being
named here.

A hotel sold out for tonight gives discovery a page with no rates on it, and
the repair rightly declines. ``--check-in`` points it at a night that has
some, which changes nothing about what is derived: selectors are not dates.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.adapters.engines import parameterise_url  # noqa: E402
from app.db.models import Hotel, HotelSource, RoomType  # noqa: E402
from app.db.session import sync_session  # noqa: E402


def _sources(match: str | None) -> list[tuple[int, str, str | None]]:
    with sync_session() as session:
        rows = session.execute(
            select(HotelSource.id, Hotel.name, HotelSource.url)
            .join(Hotel, Hotel.id == HotelSource.hotel_id)
            .order_by(Hotel.name)
        ).all()
    if match:
        needle = match.lower()
        rows = [r for r in rows if needle in r[1].lower()]
    return [(r[0], r[1], r[2]) for r in rows]


def _existing_room_names(hotel_source_id: int) -> list[str]:
    """Every room this hotel currently has on record.

    Handed to the repair as ``collapsed_names``, which is an assertion: "these
    came from a config I believe was wrong, check them." It is not taken at
    face value. ``names_to_retire`` keeps only the names the repaired config
    does NOT read back from the page, so a room that is genuinely there
    survives no matter what is passed here -- which is what makes this safe to
    point at a hotel whose rooms are mostly fine.
    """
    with sync_session() as session:
        hotel_id = session.execute(
            select(HotelSource.hotel_id).where(HotelSource.id == hotel_source_id)
        ).scalar_one_or_none()
        if hotel_id is None:
            return []
        return list(
            session.scalars(
                select(RoomType.name).where(RoomType.hotel_id == hotel_id)
            )
        )


def _clear_cooldown(hotel_source_id: int) -> str | None:
    """Hand this source a fresh repair budget.

    The cooldown and the attempt budget exist to stop an automatic repair
    driving a browser at somebody else's site every half hour forever. Neither
    reason applies to a person running this once, against one hotel, having
    just looked at the dashboard -- and waiting six hours to delete four
    competitor hotels from a client's screen is not a policy anybody chose.

    Only the repair BOOKKEEPING is cleared. The selectors are untouched, and
    the verification bar the repair applies afterwards is exactly the same:
    forcing an attempt does not force a result.
    """
    with sync_session() as session:
        row = session.execute(
            select(HotelSource).where(HotelSource.id == hotel_source_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        config = dict(row.adapter_config or {})
        state = config.pop("auto_repair", None)
        if state is None:
            return None
        row.adapter_config = config
        session.commit()
        return str(state.get("last_attempt_at") or state)


def _retemplate(hotel_source_id: int, *, dry_run: bool) -> str | None:
    """Rewrite a stored URL's dates as placeholders. Returns what changed."""
    with sync_session() as session:
        row = session.execute(
            select(HotelSource).where(HotelSource.id == hotel_source_id)
        ).scalar_one_or_none()
        if row is None or not row.url:
            return None
        templated, changed = parameterise_url(row.url)
        if templated == row.url:
            return None
        if not dry_run:
            row.url = templated
            session.commit()
        return ", ".join(f"{k}: {v}" for k, v in changed.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("hotel", nargs="?", help="part of the hotel's name")
    parser.add_argument("--all", action="store_true", help="every source")
    parser.add_argument("--list", action="store_true", help="show sources and stop")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would change; write nothing")
    parser.add_argument("--check-in", help="YYYY-MM-DD; default tomorrow")
    parser.add_argument(
        "--force", action="store_true",
        help="ignore the repair cooldown and attempt budget. The verification "
             "bar is unchanged: this forces an ATTEMPT, not a result.",
    )
    parser.add_argument(
        "--retire-unseen", action="store_true",
        help="DELETE rooms the repaired config no longer reads on the page. "
             "For a source whose old config invented rooms -- competitor "
             "hotels from a cross-sell carousel, or occupancy badges -- which "
             "otherwise sit on the dashboard forever, reported sold out.",
    )
    args = parser.parse_args()

    if not (args.hotel or args.all or args.list):
        parser.error("name a hotel, or pass --all")

    rows = _sources(None if (args.all or args.list) else args.hotel)
    if not rows:
        print(f"No source matches {args.hotel!r}.")
        return 1

    if args.list:
        for hs_id, name, url in rows:
            mark = "  " if url and "{check_in" in url else "!!"
            print(f"{mark} [{hs_id:>3}] {name[:44]:<46} {(url or '')[:60]}")
        print("\n!! = the stored URL has no date placeholder: it asks for one "
              "fixed night, every time.")
        return 0

    # Tomorrow by default: a stay that has already begun is refused by the
    # repair task itself, and tonight is often sold out by the time anyone
    # runs this.
    check_in = (
        date.fromisoformat(args.check_in) if args.check_in
        else date.today() + timedelta(days=1)
    )
    check_out = check_in + timedelta(days=1)

    # Imported here: it pulls in Playwright, and --list should not pay for a
    # browser stack it never starts.
    from app.workers.tasks_repair import rediscover_source

    failures = 0
    for hs_id, name, _url in rows:
        print("=" * 72)
        print(f"{name}  [source {hs_id}]")

        changed = _retemplate(hs_id, dry_run=args.dry_run)
        if changed:
            prefix = "would re-template" if args.dry_run else "re-templated"
            print(f"  url: {prefix} -- {changed}")

        if args.dry_run:
            print("  (dry run: discovery not run)")
            continue

        if args.force:
            cleared = _clear_cooldown(hs_id)
            if cleared:
                print(f"  force: cleared the repair cooldown (last attempt {cleared})")

        existing = _existing_room_names(hs_id) if args.retire_unseen else None
        if existing:
            print(f"  retire-unseen: offering {len(existing)} existing room name(s) "
                  f"for checking against what the page now reads")

        result = rediscover_source(
            hs_id,
            check_in=check_in.isoformat(),
            check_out=check_out.isoformat(),
            reason="manual: scripts/rediscover.py",
            collapsed_names=existing,
        )
        status = result.get("status")
        print(f"  discovery: {status}"
              + (f" -- {result.get('why')}" if result.get("why") else ""))
        retired = result.get("rooms_retired") or []
        if retired:
            print(f"    retired {len(retired)} room(s) the page does not have:")
            for name in retired:
                print(f"      - {name}")
        config = result.get("config") or {}
        if config:
            print(f"    room_card: {config.get('room_card')}")
            print(f"    selectors: {config.get('selectors')}")
        if status not in {"repaired", "no_change"}:
            failures += 1

    print("=" * 72)
    if failures:
        print(f"{failures} source(s) did not verify. Their config is UNCHANGED, "
              f"which is the point: a repair that cannot prove itself declines "
              f"rather than guessing.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
