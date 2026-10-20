"""One hotel's rates for one night: what we put in RMS, beside what Booking.com shows.

WHY THIS EXISTS
===============
After "Apply to RMS" three numbers have to agree, and they live in three
places:

    RMS            the rate this app last wrote (or read) for each plan
    the app        the guest price that rate was meant to produce
    Booking.com    the guest price our own monitor last read off the site

They are not expected to agree instantly. RMS pushes to Booking.com on its
own schedule, and a new price is only RECORDED once ``confirm_checks``
readings in a row agree -- until then it sits as "pending". So this prints
each side with its time, which is what tells "not there yet" apart from
"never arrived".

It reads this app's database only. It does not sign in to RMS -- press
Preview on the Repricing page for a live reading of the grid.

    python scripts/compare_rates.py holiday
    python scripts/compare_rates.py holiday --night 2026-10-21
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The Windows Server console is cp1252 and every rate carries a rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Hotel,
    PriceSeries,
    RepricingAction,
    RepricingSettings,
    RmsRoomMapping,
    RoomType,
    Source,
)
from app.db.session import sync_session  # noqa: E402


def _at(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    return f"{moment.astimezone(ZoneInfo(get_settings().timezone)):%d %b %H:%M} IST"


def _money(value) -> str:
    return "-" if value is None else f"₹{value:,.0f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hotel", help="Part of the hotel name, e.g. holiday")
    parser.add_argument("--night", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="Check-in night (default: tonight, IST).")
    args = parser.parse_args()

    tz = ZoneInfo(get_settings().timezone)
    night = args.night or datetime.now(tz).date()

    with sync_session() as session:
        hotels = session.scalars(
            select(Hotel).where(Hotel.name.ilike(f"%{args.hotel.strip()}%"))
        ).all()
        if not hotels:
            print(f"No hotel name contains {args.hotel!r}.")
            return 1

        for hotel in hotels:
            print(f"HOTEL {hotel.id}: {hotel.name.strip()}   night of {night:%d %b %Y}\n")
            rules = session.scalar(
                select(RepricingSettings)
                .where(RepricingSettings.owner_user_id == hotel.owner_user_id)
            )
            channel = rules.channel if rules else None

            # -- RMS: the newest action per (room, rate row) for this night ---
            print(f"RMS ({channel or 'main channel'}) -- last value this app wrote or read:")
            mappings = session.execute(
                select(RmsRoomMapping, RoomType)
                .join(RoomType, RoomType.id == RmsRoomMapping.room_type_id)
                .where(RoomType.hotel_id == hotel.id,
                       RmsRoomMapping.owner_user_id == hotel.owner_user_id)
                .order_by(RoomType.sort_order, RoomType.id)
            ).all()
            if not mappings:
                print("  no AGS room is mapped to an RMS room\n")
            for mapping, room in mappings:
                print(f"  {room.name}  ->  RMS room '{mapping.rms_room}'"
                      + ("" if mapping.is_enabled else "  (mapping DISABLED)"))
                for plan, row_name in (("EP", mapping.rate_type_ep),
                                       ("CP", mapping.rate_type_cp),
                                       ("MAP", mapping.rate_type_map)):
                    if not row_name:
                        continue
                    actions = session.scalars(
                        select(RepricingAction)
                        .where(RepricingAction.owner_user_id == hotel.owner_user_id,
                               RepricingAction.rms_room == mapping.rms_room,
                               RepricingAction.rms_rate_type == row_name,
                               RepricingAction.check_in == night)
                        .order_by(RepricingAction.created_at.desc())
                        .limit(20)
                    ).all()
                    actions = [a for a in actions if a.channel in (None, channel)]
                    if not actions:
                        print(f"     {plan:<3} no reading or write for this night")
                        continue
                    latest = actions[0]
                    # Same rule as the Repricing page: a reverted write left
                    # the cell at what it held before.
                    value = (latest.current_rms if latest.status == "reverted"
                             else (latest.applied_rms or latest.current_rms))
                    print(f"     {plan:<3} {_money(value):>9}   {latest.status} ({latest.mode}) "
                          f"at {_at(latest.created_at)}")
                    write = next((a for a in actions if a.status == "applied"), None)
                    if write is not None:
                        print(f"         last write: {_money(write.current_rms)} -> "
                              f"{_money(write.applied_rms)} at {_at(write.created_at)}, "
                              f"room's guest target {_money(write.target_price)} "
                              f"(Booking.com showed {_money(write.our_price)} then)")
            print()

            # -- Booking.com: what our own monitor last read --------------------
            print("Booking.com / monitored sites -- what our checks last read:")
            series = session.execute(
                select(PriceSeries, RoomType, Source)
                .outerjoin(RoomType, RoomType.id == PriceSeries.room_type_id)
                .join(Source, Source.id == PriceSeries.source_id)
                .where(PriceSeries.hotel_id == hotel.id, PriceSeries.check_in == night)
                .order_by(Source.id, RoomType.sort_order, PriceSeries.meal_plan)
            ).all()
            if not series:
                print(f"  nothing read for the night of {night} yet")
            for s, room, source in series:
                name = room.name if room else "(unmatched room)"
                # The site states the tax beside the price; the Repricing page
                # compares with it added, so both are printed.
                before = s.last_price_exclusive
                with_tax = s.last_price_inclusive or (
                    before + s.last_taxes_fees
                    if before is not None and s.last_taxes_fees is not None else None
                )
                line = (f"  {source.display_name[:15]:<15} {name[:28]:<28} "
                        f"{str(s.meal_plan or '')[:20]:<20} {_money(before):>9} + tax = "
                        f"{_money(with_tax):>9}")
                if not s.is_available:
                    line += "  SOLD OUT"
                if s.pending_price is not None:
                    line += (f"  PENDING {_money(s.pending_price)} "
                             f"({s.pending_count} matching check(s) since {_at(s.pending_since)})")
                line += f"   checked {_at(s.last_checked_at)}, changed {_at(s.last_changed_at)}"
                print(line)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
