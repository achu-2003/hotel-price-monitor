"""The two queries the repricing rule needs beyond tonight's prices.

Statements, not results, so the dashboard (async session) and the worker
(sync session) run the SAME query and cannot disagree about what "our usual
gap" or "already moved tonight" means. Both callers go through here.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, or_, select

from app.db.models import Hotel, PriceObservation, PriceSeries, RepricingAction, RoomType
from app.db.models.repricing import RepricingSettings
from app.services.meal_plan import ROOM_ONLY
from app.services.price_display import displayed_price
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


def previous_readings_stmt(hotel_id: int, check_in: date, check_out: date) -> Select:
    """The benchmark's last two readings of every offer for the night.

    Ranked newest first per offer key; :func:`with_previous` keeps the second.
    One night of one hotel is a few dozen rows, so this reads them whole
    rather than asking the database for exactly one row per offer.
    """
    ranked = (
        select(
            PriceObservation.offer_key,
            PriceObservation.price_exclusive,
            PriceObservation.taxes_fees,
            PriceObservation.price_inclusive,
            PriceObservation.is_available,
            func.row_number().over(
                partition_by=PriceObservation.offer_key,
                order_by=PriceObservation.checked_at.desc(),
            ).label("rank"),
        )
        .join(PriceSeries, PriceSeries.offer_key == PriceObservation.offer_key)
        .where(
            PriceSeries.hotel_id == hotel_id,
            PriceSeries.check_in == check_in,
            PriceSeries.check_out == check_out,
        )
        .subquery()
    )
    return select(ranked).where(ranked.c.rank == 2)


def with_previous(benchmark: Benchmark | None, rows) -> Benchmark | None:
    """``benchmark`` carrying the readings from :func:`previous_readings_stmt`.

    Priced on the run's own basis, so the lower of the two is a like-for-like
    comparison. A reading of a room that was not on sale is left out: a
    sold-out price is not a price Sterling was selling at.
    """
    if benchmark is None:
        return None
    previous = {}
    for row in rows:
        if not row.is_available:
            continue
        shown = displayed_price(SimpleNamespace(
            last_price_exclusive=row.price_exclusive,
            last_price_inclusive=row.price_inclusive,
            last_taxes_fees=row.taxes_fees,
            current_price=None,
        ), benchmark.with_tax)
        if shown.amount is not None:
            previous[row.offer_key] = shown.amount
    return replace(benchmark, previous=previous)


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


# -- the repricer's log, as the pages read it -------------------------
#: A rate written to RMS and read back. The one status that moved anything.
APPLIED = "applied"


def _local_day(day: date, tz: str) -> tuple[datetime, datetime]:
    """``day`` in the deployment's zone, as the UTC range it covers.

    The log is stored in UTC and read in Tamil Nadu: a change at 00:58 IST
    belongs to the 25th although it is 19:28 on the 24th in UTC.
    """
    start = datetime.combine(day, time.min, tzinfo=ZoneInfo(tz))
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def log_stmt(owner_user_id: int, *, changes_only: bool, day: date | None = None,
             tz: str = "Asia/Kolkata", limit: int = 60) -> Select:
    """The "What the repricer did" rows, newest first.

    ``changes_only`` keeps the rows that moved a rate. The full log is every
    30-minute decision, and the handful that wrote something were lost among
    the "unchanged" and "held" rows around them. Plain readings are never
    shown either way: they are figures a Preview took, not decisions.
    """
    stmt = select(RepricingAction).where(
        RepricingAction.owner_user_id == owner_user_id,
        (RepricingAction.status == APPLIED) if changes_only else (RepricingAction.status != "read"),
    )
    if day is not None:
        start, end = _local_day(day, tz)
        stmt = stmt.where(RepricingAction.created_at >= start, RepricingAction.created_at < end)
    return stmt.order_by(RepricingAction.created_at.desc(), RepricingAction.id.desc()).limit(limit)


def last_auto_change_stmt(owner_user_id: int) -> Select:
    """The newest rate the automatic mode wrote. A manual Apply is not one."""
    return (
        select(RepricingAction)
        .where(RepricingAction.owner_user_id == owner_user_id,
               RepricingAction.mode == "auto", RepricingAction.status == APPLIED)
        .order_by(RepricingAction.created_at.desc(), RepricingAction.id.desc())
        .limit(1)
    )


def auto_changes_on_stmt(owner_user_id: int, day: date, tz: str = "Asia/Kolkata") -> Select:
    """How many rates the automatic mode wrote on ``day``, local time."""
    start, end = _local_day(day, tz)
    return select(func.count()).select_from(RepricingAction).where(
        RepricingAction.owner_user_id == owner_user_id,
        RepricingAction.mode == "auto", RepricingAction.status == APPLIED,
        RepricingAction.created_at >= start, RepricingAction.created_at < end,
    )
