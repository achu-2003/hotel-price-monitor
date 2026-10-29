"""Tonight's prices for our own hotel, as each platform shows them.

Sales Daddy's WhatsApp AI answers "how much is the Deluxe tonight?" from its
own product list and nothing else -- it cannot call out to us. So the list is
filled from here: one product per room per platform, carrying the price the
platform printed on our last check of it.

THE PLATFORM'S NUMBER, NOT OURS. The Repricing page's "Guest will see" is
what a rate written to RMS ought to come out at; this is what the platform
actually showed when it was last read. The two differ exactly when something
has gone wrong (Agoda priced from the Booking.com rows, a rename), and a
guest who opens the site sees the second.

EVERY ROOM, MAPPED OR NOT. The RMS mapping is what lets us WRITE a room's
rate; reading its price needs none. A room mapped next month was already
being quoted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select

from app.db.models import Hotel, PriceSeries, RoomType, Source

#: Adults the dashboard and the repricer price a room for. The feed quotes the
#: same offers, so its numbers are the ones on those pages.
ADULTS = 2


@dataclass(frozen=True)
class Row:
    """One offer of tonight's, with the names it is quoted under."""

    room_type_id: int
    room: str
    source_code: str
    channel: str
    meal_plan: str | None
    price: Decimal | None
    price_with_tax: Decimal | None
    currency: str
    available: bool
    seen_at: datetime


def rows_stmt(hotel_id: int, night: date):
    return (
        select(PriceSeries, RoomType.name, Source.code, Source.display_name)
        .join(RoomType, PriceSeries.room_type_id == RoomType.id)
        .join(Source, PriceSeries.source_id == Source.id)
        .join(Hotel, PriceSeries.hotel_id == Hotel.id)
        .where(
            PriceSeries.hotel_id == hotel_id,
            PriceSeries.check_in == night,
            PriceSeries.check_out == date.fromordinal(night.toordinal() + 1),
            PriceSeries.adults == ADULTS,
            RoomType.is_active.is_(True),
            Hotel.is_active.is_(True),
            Hotel.is_own_property.is_(True),
        )
        .order_by(Source.display_name, RoomType.sort_order, RoomType.name)
    )


def to_rows(results: Iterable[tuple]) -> list[Row]:
    return [
        Row(
            room_type_id=series.room_type_id, room=room, source_code=code, channel=channel,
            meal_plan=series.meal_plan, price=series.current_price,
            price_with_tax=series.last_price_inclusive, currency=series.currency,
            available=series.is_available and series.current_price is not None,
            seen_at=series.last_checked_at,
        )
        for series, room, code, channel in results
    ]


def items(hotel_id: int, rows: Iterable[Row]) -> list[dict[str, Any]]:
    """One product per room per platform: its cheapest offer on sale tonight.

    A room sells under several boards and cancellation terms, and the AI is
    asked for "the price", which a guest hears as the lowest one. A room with
    nothing on sale is still sent, as sold out -- dropping it would leave
    Sales Daddy quoting the last price it had.
    """
    best: dict[tuple[int, str], Row] = {}
    for row in rows:
        key = (row.room_type_id, row.source_code)
        kept = best.get(key)
        if kept is None or _cheaper(row, kept):
            best[key] = row
    return [
        {
            "external_id": f"h{hotel_id}-r{row.room_type_id}-{row.source_code}",
            "name": f"{row.room} – {row.channel} – tonight",
            "room": row.room,
            "channel": row.channel,
            "available": row.available,
            "price": _money(row.price) if row.available else None,
            "price_with_tax": _money(row.price_with_tax) if row.available else None,
            "meal_plan": row.meal_plan,
            "currency": row.currency,
            "description": _description(row),
            "seen_at": row.seen_at.isoformat(),
        }
        for row in best.values()
    ]


#: Bumped when a field changes meaning or goes away -- never for an addition.
#: The receiver is somebody else's code; docs/SALESDADDY_PRICE_FEED.md is the contract.
VERSION = 1


def payload(hotel: Hotel, night: date, products: list[dict[str, Any]]) -> dict[str, Any]:
    """One hotel's list for one night.

    ``complete``: these are ALL of the hotel's products under ``source`` -- an
    ``external_id`` the receiver holds that is not here is gone (a room renamed
    on a platform) and should stop being quoted. Always true today; the field
    exists so a partial send can never be mistaken for a full one later.
    """
    return {"version": VERSION, "source": "rms", "hotel_id": hotel.id, "hotel": hotel.name,
            "night": night.isoformat(), "complete": True, "items": products}


def fingerprint(body: dict[str, Any]) -> str:
    """What has to change for the list to be worth sending again.

    Not ``seen_at``: every check moves it, and a list re-sent because the
    clock moved is a call per fetch for nothing. The periodic resend is what
    keeps Sales Daddy's copy fresh (Settings.price_feed_resend_hours).
    """
    stable = {**body, "items": [{k: v for k, v in i.items() if k != "seen_at"} for i in body["items"]]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _description(row: Row) -> str:
    """The product in a sentence a chat assistant can repeat without guessing."""
    board = row.meal_plan or "room only"
    if not row.available:
        return f"Sold out tonight on {row.channel}."
    with_tax = (f"₹{_money(row.price_with_tax):,} including taxes"
                if row.price_with_tax is not None else f"₹{_money(row.price):,} (taxes may be extra)")
    return f"Tonight on {row.channel}, 2 adults, {board}: {with_tax}."


def _cheaper(row: Row, kept: Row) -> bool:
    if row.available != kept.available:
        return row.available
    if not row.available:
        return row.seen_at > kept.seen_at
    return row.price < kept.price


def _money(amount: Decimal | None) -> int | None:
    return None if amount is None else int(amount.quantize(Decimal("1")))
