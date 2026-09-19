"""The two queries the repricing rule needs beyond tonight's prices.

Statements, not results, so the dashboard (async session) and the worker
(sync session) run the SAME query and cannot disagree about what "our usual
gap" or "already moved tonight" means. Both callers go through here.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import Select, or_, select

from app.db.models import Hotel, PriceSeries, RepricingAction, RoomType
from app.services.repricing import HISTORY_NIGHTS


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
