"""Apply the day-over-day comparison to series that predate the feature.

WHY THIS EXISTS
===============
The carry-over comparison runs at FIRST SIGHTING of a stay date: that is what
guarantees at most one overnight alert per series no matter how often the
target is checked. The consequence is that stay dates already first-sighted
before the feature shipped were never compared to their predecessor, and never
will be — their first sighting is in the past.

This script performs that missed comparison once. It is not a routine tool;
after the first run there is nothing left for it to find, because every
subsequent stay date is first-sighted by the live pipeline.

Dry-run by default. Nothing is written without --apply.

    python scripts/backfill_carry_over.py                 # show what would fire
    python scripts/backfill_carry_over.py --apply         # write the change rows
    python scripts/backfill_carry_over.py --since 2026-08-19

Existing carry-over rows are skipped, so re-running is safe and idempotent.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The Windows console defaults to cp1252, which cannot encode the rupee
# sign this script prints on every line.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import and_, select  # noqa: E402

from app.db.models import Hotel, PriceChange, PriceSeries, RoomType  # noqa: E402
from app.db.session import sync_session  # noqa: E402
from app.services import monitoring  # noqa: E402
from app.services.comparison import (  # noqa: E402
    CarryOver,
    Observation,
    compare_across_stay_dates,
)


def _previous_series(session, series: PriceSeries) -> PriceSeries | None:
    """The same room and booking conditions, most recent earlier stay date.

    Mirrors ``ingest._previous_stay_series`` exactly, including the IS NULL
    handling for meal_plan and refundable — a ``= NULL`` here would match
    nothing and the backfill would silently report zero work to do.
    """
    nights = (series.check_out - series.check_in).days
    conditions = [
        PriceSeries.hotel_id == series.hotel_id,
        PriceSeries.source_id == series.source_id,
        PriceSeries.room_type_id == series.room_type_id,
        PriceSeries.adults == series.adults,
        PriceSeries.children == series.children,
        PriceSeries.currency == series.currency,
        PriceSeries.check_in < series.check_in,
        (PriceSeries.check_out - PriceSeries.check_in) == nights,
        PriceSeries.meal_plan.is_(None)
        if series.meal_plan is None
        else PriceSeries.meal_plan == series.meal_plan,
        PriceSeries.refundable.is_(None)
        if series.refundable is None
        else PriceSeries.refundable == series.refundable,
    ]
    return session.execute(
        select(PriceSeries)
        .where(and_(*conditions))
        .order_by(PriceSeries.check_in.desc())
        .limit(1)
    ).scalar_one_or_none()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="write the change rows (default is a dry run)",
    )
    parser.add_argument(
        "--since", type=date.fromisoformat, default=None,
        help="only consider stay dates on or after this ISO date",
    )
    args = parser.parse_args()

    thresholds = monitoring.default_thresholds()
    print(
        f"thresholds: ₹{thresholds.min_delta_abs} and {thresholds.min_delta_pct}% "
        f"(a move must clear BOTH)\n"
    )

    written = 0
    skipped_existing = 0
    considered = 0

    with sync_session() as session:
        statement = select(PriceSeries).order_by(PriceSeries.check_in, PriceSeries.offer_key)
        if args.since is not None:
            statement = statement.where(PriceSeries.check_in >= args.since)

        for series in session.execute(statement).scalars():
            previous = _previous_series(session, series)
            if previous is None:
                continue
            considered += 1

            change = compare_across_stay_dates(
                CarryOver(
                    last_price=previous.last_price,
                    is_available=previous.is_available,
                    check_in=previous.check_in,
                    offer_key=previous.offer_key,
                ),
                Observation(price=series.last_price, is_available=series.is_available),
                thresholds,
                this_check_in=series.check_in,
            )
            if change is None:
                continue

            already = session.execute(
                select(PriceChange.id).where(
                    PriceChange.offer_key == series.offer_key,
                    PriceChange.previous_offer_key == change.previous_offer_key,
                )
            ).first()
            if already is not None:
                skipped_existing += 1
                continue

            hotel = session.get(Hotel, series.hotel_id)
            room = session.get(RoomType, series.room_type_id)
            sign = "+" if change.direction.value == "increase" else "−"
            print(
                f"  {hotel.name if hotel else series.hotel_id} · "
                f"{room.name if room else series.room_type_id}\n"
                f"      {change.previous_check_in} → {series.check_in}   "
                f"{change.old_price} → {change.new_price}   "
                f"{sign}{abs(change.delta)} ({change.delta_pct}%)"
            )

            if args.apply:
                session.add(
                    PriceChange(
                        offer_key=series.offer_key,
                        hotel_id=series.hotel_id,
                        # The series' own last check is when this price was
                        # observed. Stamping "now" would claim the hotel moved
                        # its rate at backfill time, which it did not.
                        changed_at=series.last_checked_at,
                        old_price=change.old_price,
                        new_price=change.new_price,
                        delta=change.delta,
                        delta_pct=change.delta_pct,
                        currency=series.currency,
                        direction=change.direction,
                        previous_offer_key=change.previous_offer_key,
                        notified=False,
                    )
                )
            written += 1

        if args.apply:
            session.commit()

    print(
        f"\n{considered} series had an earlier stay date to compare against.\n"
        f"{written} change{'' if written == 1 else 's'} "
        f"{'written' if args.apply else 'would be written'}."
    )
    if skipped_existing:
        print(f"{skipped_existing} already had a carry-over row and were skipped.")
    if written and not args.apply:
        print("\nRe-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
