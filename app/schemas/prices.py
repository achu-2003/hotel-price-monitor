"""Current prices, history, changes, and the comparison matrix."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.db.models.enums import ChangeDirection, PriceBasis
from app.schemas.common import ORMModel


class CurrentPriceOut(ORMModel):
    """One live price, read from ``price_series``.

    ``offer_key`` is exposed because it is the handle for everything else: the
    history endpoint takes it, and it is the only unambiguous way to refer to
    one comparable price.
    """

    offer_key: str
    hotel_id: int
    hotel_name: str | None = None
    room_type_id: int
    room_name: str | None = None
    source_id: int
    check_in: date
    check_out: date
    adults: int
    children: int
    meal_plan: str | None
    refundable: bool | None
    currency: str
    current_price: Decimal | None = Field(
        default=None,
        description="What the source is asking as of the last check. This is "
                    "the number to display: it tracks every check, including "
                    "moves too small to alert on.",
    )
    last_price: Decimal | None = Field(
        default=None,
        description="The confirmed baseline the change detector compares "
                    "against. Deliberately does not move for a change below "
                    "the alert threshold, so successive small drifts "
                    "accumulate against one fixed point. Not a display value.",
    )
    last_price_basis: PriceBasis
    is_available: bool
    first_seen_at: datetime
    last_checked_at: datetime
    last_changed_at: datetime | None
    pending_price: Decimal | None = Field(
        default=None,
        description="A price that has moved enough to matter but has not yet "
                    "survived the confirmation checks. Shown so the dashboard "
                    "can indicate a change in progress without alerting on it.",
    )
    pending_count: int = 0
    is_stale: bool = Field(
        default=False,
        description="No successful check in three intervals. Frozen prices "
                    "that still look current are the expensive failure mode.",
    )


class HistoryPoint(ORMModel):
    """One point on a price chart."""

    checked_at: datetime
    price_inclusive: Decimal | None
    price_exclusive: Decimal | None
    is_available: bool
    rooms_left: int | None = None


class HistoryOut(ORMModel):
    offer_key: str
    hotel_name: str | None
    room_name: str | None
    check_in: date
    check_out: date
    currency: str
    bucket: Literal["raw", "hourly", "daily"]
    points: list[HistoryPoint]


class PriceChangeOut(ORMModel):
    id: int
    offer_key: str
    hotel_id: int
    hotel_name: str | None = None
    room_name: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    changed_at: datetime
    old_price: Decimal | None
    new_price: Decimal | None
    delta: Decimal | None
    delta_pct: Decimal | None
    currency: str
    direction: ChangeDirection
    notified: bool


class MatrixCell(ORMModel):
    room_name: str
    offer_key: str
    price: Decimal | None
    is_available: bool
    last_checked_at: datetime
    changed_recently: bool = False


class MatrixRow(ORMModel):
    hotel_id: int
    hotel_name: str
    is_own_property: bool
    cells: list[MatrixCell]
    cheapest: Decimal | None = None


class MatrixOut(ORMModel):
    """All hotels x rooms for one stay window.

    The screen the whole system exists to produce: what is everyone charging
    for this night, right now.
    """

    check_in: date
    check_out: date
    adults: int
    children: int
    currency: str
    rows: list[MatrixRow]
    generated_at: datetime


class UnmatchedOfferOut(ORMModel):
    """A room name nothing resolved, waiting for a human decision.

    The system will not guess below the confidence threshold: a wrong mapping
    corrupts a price series invisibly and indefinitely, while this queue is a
    visible gap that gets cleared once and then never recurs.
    """

    id: int
    hotel_source_id: int
    hotel_id: int | None = None
    hotel_name: str | None = None
    raw_room_name: str
    normalized_name: str
    suggested_room_type_id: int | None
    suggested_room_name: str | None = None
    suggested_confidence: float | None
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    resolved_at: datetime | None


class ResolveUnmatchedIn(ORMModel):
    """Map an unmatched room name to a room type, once and for all.

    Writes a ``manual`` alias, which outranks fuzzy matching from then on.
    """

    room_type_id: int


class DashboardSummary(ORMModel):
    hotels_active: int
    targets_enabled: int
    circuits_open: int
    checks_last_hour: int
    changes_last_24h: int
    unmatched_rooms: int
    unresolved_errors: int
    stale_targets: int
    notifications_failed_24h: int
    oldest_successful_check: datetime | None
