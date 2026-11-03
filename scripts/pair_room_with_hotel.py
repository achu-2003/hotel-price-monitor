"""Pair ONE room of ours with a room of another hotel than the benchmark, on one website.

The other pairs, the gap, the board, the limits and the automatic switches are
left exactly as they are: only this room's pair is added or replaced. That is
the difference from ``copy_room_mappings.py --import --settings``, which
replaces the whole rule with the copy from another machine.

Everything is named, not numbered -- hotel, room and website ids are per
database -- so the same command works locally and on the server::

    python scripts/pair_room_with_hotel.py --channel Booking.com --room "Family Suite" \\
        --hotel "Ananthyam Resort" --theirs "Family Room" --board "Room Only"
    python scripts/pair_room_with_hotel.py --channel Agoda --room "Family Suite" \\
        --ours "Suite Room" --hotel "Ananthyam Resort" --theirs "Family Room"

It prints what it would change; add ``--yes`` to write. Running it twice is
safe: the second run finds the pair already there.

``--channel`` is the RMS channel: the main rule's (Booking.com) or one of the
other websites' rules (Agoda, Goibibo). ``--ours`` is our room's name on that
website when it is not the room's own name. ``--board`` compares this room on
another board than the rule's, both sides.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.db.models import Hotel, RoomType  # noqa: E402
from app.db.models.repricing import RepricingChannelRule, RepricingSettings  # noqa: E402
from app.db.session import sync_session  # noqa: E402


def _named(session, model, name: str, *where):
    rows = session.scalars(select(model).where(
        func.lower(func.trim(model.name)) == " ".join(name.split()).lower(), *where)).all()
    return rows[0] if len(rows) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--channel", required=True)
    ap.add_argument("--room", required=True, help="our room, by its name on the Repricing page")
    ap.add_argument("--hotel", required=True, help="the competitor this room follows")
    ap.add_argument("--theirs", required=True, help="their room, by its name on that website")
    ap.add_argument("--ours", help="our room's name on that website, when it differs")
    ap.add_argument("--board", help='compare on this board instead of the rule\'s, e.g. "Room Only"')
    ap.add_argument("--yes", action="store_true", help="write it")
    a = ap.parse_args()

    with sync_session() as s:
        own = s.scalar(select(Hotel).where(Hotel.is_own_property.is_(True), Hotel.is_active.is_(True)))
        if own is None:
            print("No active own property here."); return 1
        room = _named(s, RoomType, a.room, RoomType.hotel_id == own.id, RoomType.is_active.is_(True))
        hotel = _named(s, Hotel, a.hotel, Hotel.owner_user_id == own.owner_user_id,
                       Hotel.is_active.is_(True), Hotel.is_own_property.is_(False))
        if room is None or hotel is None:
            print(f"Not found here: {'room ' + repr(a.room) if room is None else ''} "
                  f"{'competitor ' + repr(a.hotel) if hotel is None else ''}".strip()); return 1
        settings = s.scalar(select(RepricingSettings).where(
            RepricingSettings.owner_user_id == own.owner_user_id))
        if settings is None:
            print("This account has no repricing rule yet."); return 1

        key = str(room.id)
        if a.channel.strip().lower() == (settings.channel or "").lower():
            row, column, pair = settings, "benchmark_room_pairs", {"theirs": a.theirs, "hotel_id": hotel.id}
        else:
            row = s.scalar(select(RepricingChannelRule).where(
                RepricingChannelRule.owner_user_id == own.owner_user_id,
                func.lower(RepricingChannelRule.channel) == a.channel.strip().lower()))
            if row is None:
                print(f"No rule for {a.channel} here. Add it on the Repricing page first."); return 1
            column = "room_pairs"
            pair = {"ours": a.ours, "theirs": a.theirs, "hotel_id": hotel.id}
        if a.board:
            pair["board"] = a.board

        stored = dict(getattr(row, column) or {})
        before = stored.get(key)
        print(f"{a.channel} / {room.name} (room {room.id}) follows {hotel.name} (hotel {hotel.id})")
        print(f"  before: {before}")
        print(f"  after:  {pair}")
        if before == pair:
            print("Already set; nothing to do."); return 0
        stored[key] = pair
        setattr(row, column, stored)
        flag_modified(row, column)
        if a.yes:
            s.commit(); print("Written.")
        else:
            s.rollback(); print("Nothing written; run again with --yes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
