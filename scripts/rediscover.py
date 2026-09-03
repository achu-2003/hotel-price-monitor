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
from app.db.models import Hotel, HotelSource  # noqa: E402
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

        result = rediscover_source(
            hs_id,
            check_in=check_in.isoformat(),
            check_out=check_out.isoformat(),
            reason="manual: scripts/rediscover.py",
        )
        status = result.get("status")
        print(f"  discovery: {status}"
              + (f" -- {result.get('why')}" if result.get("why") else ""))
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
