"""What selectors a source is running on, and what a fresh scan would replace them with.

WHY THIS EXISTS
===============
A source's ``adapter_config`` is written by discovery with nobody reading the
result, and the note it leaves behind can describe a failure in the vocabulary
of a success. This one is real, from Treebo Emerald Dove on 3 Sep 2026:

    "Auto-repaired 2026-09-03: 1 rooms, 1/1 prices confirmed against the
     page, 1 of them printed with a currency."

Every clause of that is true and the config it describes had lost three of the
hotel's four rooms, naming the survivor after the property. Nothing downstream
could tell: the price was on the page, it had a currency beside it, and every
check since has confirmed that a number on that page is a number on that page.

So this prints the stored config beside the rooms the source is ACTUALLY
producing. Two lines that disagree -- one room type against a config claiming
to read a room list -- is the shape of the fault, and it is visible here and
nowhere else.

    python scripts/inspect_source_config.py                 # every source
    python scripts/inspect_source_config.py --hotel EMERALD # one of them

READING IS THE DEFAULT. ``--repair`` drives a real browser against the live
page and, if what comes back is not a regression, writes it. That is the same
code path auto-repair uses, run deliberately and with the outcome printed, and
it answers the only question a passive guard cannot: what does discovery
actually see on that page today.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The Windows Server console is cp1252 and room names carry rupee signs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import distinct, select  # noqa: E402

from app.db.models import (  # noqa: E402
    Hotel,
    HotelSource,
    PriceSeries,
    RoomType,
    Source,
)
from app.db.session import sync_session  # noqa: E402
from app.services.rediscovery import names_echo_the_property  # noqa: E402


def _rooms_for(session, hotel_id: int, source_id: int) -> list[str]:
    """The room names this source is actually producing, not the ones it claims."""
    return list(
        session.scalars(
            select(distinct(RoomType.name))
            .join(PriceSeries, PriceSeries.room_type_id == RoomType.id)
            .where(
                PriceSeries.hotel_id == hotel_id,
                PriceSeries.source_id == source_id,
            )
            .order_by(RoomType.name)
        ).all()
    )


def _looks_wrong(hotel_name: str, rooms: list[str]) -> str | None:
    """A room named after its property, judged by the guard's own rule.

    Deliberately ``names_echo_the_property`` rather than a comparison written
    here. The first version of this function compared the first fourteen
    characters of each name, and it did not flag Treebo Emerald Dove -- the
    one source known to be broken -- because the stored hotel name is spelled
    PREMIMUM against a page saying Premium. A tool that reports on a guard
    must apply that guard, or it certifies as healthy exactly what the guard
    would reject.

    Asked one room at a time. The guard requires EVERY name to echo before it
    declines a repair, which is right when the question is "should this config
    be written"; here the question is "is anything in this list wrong", and one
    bad room among several is still one bad room.
    """
    for room in rooms:
        if names_echo_the_property(hotel_name, [room]):
            return f"a room named after the property: {room!r}"
    return None


def _worth_a_look(rooms: list[str]) -> str | None:
    """Softer than SUSPECT, because a small property really does have one room.

    A single room type can mean a card selector that landed on a container.
    It can equally mean a Treebo property with one room, which is what all
    three of them are. Reported, not accused.
    """
    if len(rooms) == 1:
        return "one room type only -- fine for a small property, worth a look otherwise"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hotel", help="Only sources whose hotel name matches this.")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Drive a real browser and re-derive the config. Writes if it verifies.",
    )
    args = parser.parse_args()

    with sync_session() as session:
        query = (
            select(HotelSource, Hotel.name, Source.display_name, Source.adapter_key)
            .join(Hotel, Hotel.id == HotelSource.hotel_id)
            .join(Source, Source.id == HotelSource.source_id)
            .order_by(Hotel.name)
        )
        if args.hotel:
            query = query.where(Hotel.name.ilike(f"%{args.hotel}%"))
        rows = session.execute(query).all()

        if not rows:
            print("No source matched.")
            return 1

        suspect = []
        for hs, hotel_name, source_name, adapter in rows:
            rooms = _rooms_for(session, hs.hotel_id, hs.source_id)
            config = hs.adapter_config or {}
            note = config.get("discovery_note") or "(never discovered)"
            warning = _looks_wrong(hotel_name, rooms)

            print("=" * 74)
            print(f"{hotel_name}")
            print(f"  source        {source_name}  [{adapter}]  id={hs.id}")
            print(f"  note          {note}")
            print(f"  room_card     {config.get('room_card')!r}")
            print(f"  room_name     {(config.get('selectors') or {}).get('room_name')!r}")
            print(f"  price         {(config.get('selectors') or {}).get('price')!r}")
            print(f"  rooms read    {len(rooms)}: {', '.join(rooms[:6]) or '(none)'}")
            if warning:
                print(f"  ⚠  SUSPECT    {warning}")
                suspect.append((hs.id, hotel_name))
            elif _worth_a_look(rooms):
                print(f"  ·  note       {_worth_a_look(rooms)}")
            print()

        if suspect:
            print(f"{len(suspect)} source(s) look wrong:")
            for sid, name in suspect:
                print(f"   id={sid}  {name}")
            print()

    if not args.repair:
        print("Read-only. Pass --repair to re-derive a config against the live page.")
        return 0

    if len(rows) != 1:
        print("--repair works on ONE source at a time. Narrow it with --hotel.")
        return 1

    # Imported here, not at module scope: it pulls in Playwright.
    from app.workers.tasks_repair import rediscover_source

    hs = rows[0][0]
    # Tomorrow, because discovery refuses a window that has already begun and a
    # page for a past night renders no rooms to learn from.
    check_in = date.today() + timedelta(days=1)
    print(f"Scanning {rows[0][1]} for {check_in} -- this drives a browser, give it a minute.\n")

    outcome = rediscover_source(
        hs.id,
        check_in=check_in.isoformat(),
        check_out=(check_in + timedelta(days=1)).isoformat(),
        reason="operator ran scripts/inspect_source_config.py --repair",
    )
    print("OUTCOME:", json.dumps(outcome, indent=2, default=str))

    if outcome.get("status") == "regressed":
        print(
            "\nDeclined, and the stored config is untouched. Discovery still cannot\n"
            "read this page properly -- it found fewer rooms than the source already\n"
            "produces. This is the guard working; the page needs a person."
        )
    elif outcome.get("status") in {"repaired", "ok", "updated"}:
        print("\nWritten. Re-run without --repair to see the new selectors and rooms.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
