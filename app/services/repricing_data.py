"""The two queries the repricing rule needs beyond tonight's prices.

Statements, not results, so the dashboard (async session) and the worker
(sync session) run the SAME query and cannot disagree about what "our usual
gap" or "already moved tonight" means. Both callers go through here.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Select, or_, select

from app.db.models import Hotel, PriceSeries, RepricingAction, RoomType
from app.db.models.repricing import RepricingSettings
from app.services.meal_plan import ROOM_ONLY
from app.services.repricing import HISTORY_NIGHTS, Benchmark


def history_stmt(owner_user_id: int, night: date, adults: int = 2) -> Select:
    """The matrix rows for the :data:`HISTORY_NIGHTS` nights before ``night``.

    Same filters as tonight's query -- the owner's active hotels and rooms --
    so the usual gap is measured against the same market it is applied to.
    Rows for stays longer than one night come back too; :func:`one_night`
    drops them.
    """
    return (
        select(PriceSeries, Hotel, RoomType.name)
        .join(Hotel, PriceSeries.hotel_id == Hotel.id)
        .join(RoomType, PriceSeries.room_type_id == RoomType.id)
        .where(
            PriceSeries.check_in >= night - timedelta(days=HISTORY_NIGHTS),
            PriceSeries.check_in < night,
            PriceSeries.adults == adults,
            Hotel.is_active.is_(True),
            RoomType.is_active.is_(True),
            Hotel.owner_user_id == owner_user_id,
        )
    )


def one_night(rows) -> list:
    return [r for r in rows if r[0].check_out == r[0].check_in + timedelta(days=1)]


def pinned_board_stmt(owner_user_id: int) -> Select:
    """The meal plan this owner's comparison is pinned to, if any.

    Here beside the other two for the same reason they are: the Changes page
    reads it on an async session and the dispatcher on a sync one, and a
    screen that collapsed a room's boards differently from the alert about
    that room would be two answers to "how many rooms moved tonight".

    ``None`` -- no repricing row, or no board pinned -- means room-only wins,
    which is what :func:`app.services.room_moves.one_per_room` does with it.
    """
    return select(RepricingSettings.benchmark_meal_plan).where(
        RepricingSettings.owner_user_id == owner_user_id
    )


def benchmark_stmt(owner_user_id: int, hotel_id: int | None) -> Select:
    """The benchmarked hotel, if it is still one this owner may price against.

    RE-CHECKED, never taken from the settings row alone. The id was written
    when somebody picked it from a list, and a hotel can be deactivated,
    marked as the owner's own property, or handed to another account after
    that. Every one of those turns a stored id into a rate written against
    a hotel this owner is not competing with -- and the last one writes it
    against another account's data.
    """
    return select(Hotel.id, Hotel.name).where(
        Hotel.id == hotel_id,
        Hotel.owner_user_id == owner_user_id,
        Hotel.is_active.is_(True),
        Hotel.is_own_property.is_(False),
    )


def benchmark_for(row, settings) -> Benchmark | None:
    """The row from :func:`benchmark_stmt` plus the settings, as the rule wants it."""
    if row is None:
        return None
    return Benchmark(hotel_id=row.id, hotel=row.name,
                     undercut=Decimal(settings.benchmark_undercut),
                     with_tax=bool(settings.benchmark_with_tax),
                     meal_plan=settings.benchmark_meal_plan or None,
                     # A room of ours the chosen board does not sell drops
                     # to room-only; its rival stays on the chosen board. Implicit
                     # rather than another switch: an owner who pins a board
                     # wants the rooms that have it compared on it, not the
                     # rooms that do not left unpriced.
                     fallback_board=(ROOM_ONLY if settings.benchmark_meal_plan else None),
                     room_pairs={int(k): v for k, v in
                                 (settings.benchmark_room_pairs or {}).items()})


def moved_stmt(owner_user_id: int, night: date, channel: str) -> Select:
    """Rooms whose MAIN-channel rate was written for ``night`` already -- by the rule or by hand.

    Only ``channel`` counts (and rows from before channels were recorded,
    which were all it): a Goibibo rate the owner ticked is not the rule's
    one move of the night, and must not hold the main channel's.
    """
    return select(RepricingAction.room_type_id).where(
        RepricingAction.owner_user_id == owner_user_id,
        RepricingAction.check_in == night,
        RepricingAction.status == "applied",
        RepricingAction.room_type_id.isnot(None),
        or_(RepricingAction.channel == channel, RepricingAction.channel.is_(None)),
    ).distinct()
