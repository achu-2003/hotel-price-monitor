"""The two queries the repricing rule needs beyond tonight's prices.

Statements, not results, so the dashboard (async session) and the worker
(sync session) run the SAME query and cannot disagree about what "our usual
gap" or "already moved tonight" means. Both callers go through here.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, or_, select

from app.db.models import Hotel, PriceObservation, PriceSeries, RepricingAction, RoomType
from app.db.models.repricing import RepricingChannelRule, RepricingSettings
from app.services.meal_plan import ROOM_ONLY
from app.services.price_display import displayed_price
from app.services.repricing import HISTORY_NIGHTS, Benchmark
from app.services.room_matching import normalize_room_name


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


def on_channel(channel: str, *, main: bool = True):
    """The log rows that belong to ``channel``.

    Rows from before channels were recorded carry NULL, and every one of
    them was the main channel's -- so NULL counts for the main channel and
    for no other. Counting it for Agoda would hand Agoda's ratio guard a
    Booking.com write to pair against.
    """
    if main:
        return or_(RepricingAction.channel == channel, RepricingAction.channel.is_(None))
    return RepricingAction.channel == channel


def moved_stmt(owner_user_id: int, night: date, channel: str, *, main: bool = True) -> Select:
    """Rooms whose rate on ``channel`` was written for ``night`` already -- by the rule or by hand.

    Only ``channel`` counts (and, for the main channel, rows from before
    channels were recorded, which were all it): a Goibibo rate the owner
    ticked is not the rule's one move of the night, and must not hold the
    main channel's.
    """
    return select(RepricingAction.room_type_id).where(
        RepricingAction.owner_user_id == owner_user_id,
        RepricingAction.check_in == night,
        RepricingAction.status == "applied",
        RepricingAction.room_type_id.isnot(None),
        on_channel(channel, main=main),
    ).distinct()


# -- one channel's rule: which site, how far under, which rooms ------------
@dataclass(frozen=True, slots=True)
class Lane:
    """One RMS channel and everything that decides its rate.

    The main channel (Booking.com) is a lane built from
    :class:`RepricingSettings`; every extra channel is one built from a
    :class:`RepricingChannelRule`. The rule itself (``services.repricing``)
    does not know the difference -- it is handed the lane's rows and the
    lane's benchmark -- so Agoda is priced by exactly the arithmetic that
    has been pricing Booking.com.
    """

    channel: str
    #: The site our price and the benchmark's are read from. ``None`` keeps
    #: the old behaviour (our cheapest on any site), only for a main channel
    #: nobody has pinned.
    source_id: int | None
    main: bool
    auto: bool
    undercut: Decimal = Decimal("100")
    with_tax: bool = True
    meal_plan: str | None = None
    #: ``{our room_type_id: their room name}``, as :class:`Benchmark` wants.
    pairs: Mapping[int, str] = field(default_factory=dict)
    #: ``{our room_type_id: our room's name on this site}``, where it is not
    #: the room's own name. See :func:`lane_rows`.
    ours: Mapping[int, str] = field(default_factory=dict)


def main_lane(settings) -> Lane:
    return Lane(
        channel=settings.channel, source_id=getattr(settings, "source_id", None), main=True,
        auto=bool(getattr(settings, "auto_enabled", False)),
        undercut=Decimal(settings.benchmark_undercut),
        with_tax=bool(settings.benchmark_with_tax),
        meal_plan=settings.benchmark_meal_plan or None,
        pairs={int(k): v for k, v in (settings.benchmark_room_pairs or {}).items()},
    )


def extra_lane(rule_row) -> Lane:
    pairs: dict[int, str] = {}
    ours: dict[int, str] = {}
    for key, pair in (rule_row.room_pairs or {}).items():
        if not isinstance(pair, dict):
            continue
        if (pair.get("theirs") or "").strip():
            pairs[int(key)] = pair["theirs"].strip()
        if (pair.get("ours") or "").strip():
            ours[int(key)] = pair["ours"].strip()
    return Lane(
        channel=rule_row.channel, source_id=rule_row.source_id, main=False,
        auto=bool(rule_row.auto_enabled),
        undercut=Decimal(rule_row.benchmark_undercut),
        with_tax=bool(rule_row.benchmark_with_tax),
        meal_plan=rule_row.benchmark_meal_plan or None,
        pairs=pairs, ours=ours,
    )


def channel_rules_stmt(owner_user_id: int) -> Select:
    return (select(RepricingChannelRule)
            .where(RepricingChannelRule.owner_user_id == owner_user_id)
            .order_by(RepricingChannelRule.channel))


class _AsRoom:
    """A price series read as one of OUR rooms, without touching the row.

    The series is a live ORM object; setting its ``room_type_id`` would be a
    write the next commit carries to the database. Everything is read
    through, except the one field this lane says otherwise about.
    """

    __slots__ = ("_series", "room_type_id")

    def __init__(self, series, room_type_id: int):
        self._series = series
        self.room_type_id = room_type_id

    def __getattr__(self, name):
        return getattr(self._series, name)


def lane_rows(rows, own_hotel_id: int, lane: Lane) -> list:
    """Tonight's rows as ``lane`` sees them.

    OUR SIDE COMES FROM THE LANE'S SITE AND NO OTHER. The rule takes our
    cheapest price, and our cheapest across two sites is a price on whichever
    is cheaper tonight -- after which the Booking.com row in RMS would be
    scaled by Agoda's discount. The competitors are left alone: the
    benchmark is already read on the site our price came from, and the
    median rule wants the whole market.

    OUR ROOM UNDER THE SITE'S NAME. Agoda calls ASG's "Deluxe Double Room"
    "Deluxe Room", which this system files as a room of its own, with no RMS
    row. ``lane.ours`` says which of the site's rooms each mapped room is,
    and those rows are read as the mapped room. A mapped room whose site
    name is given takes ONLY that name: another room that happens to share
    its id is not what the owner pointed at.
    """
    wanted = {normalize_room_name(name): room_type_id for room_type_id, name in lane.ours.items()}
    out = []
    for series, hotel, room_name in rows:
        if hotel.id != own_hotel_id:
            out.append((series, hotel, room_name))
            continue
        if lane.source_id is not None and series.source_id != lane.source_id:
            continue
        as_room = wanted.get(normalize_room_name(room_name))
        if as_room is not None:
            if as_room != series.room_type_id:
                series = _AsRoom(series, as_room)
        elif series.room_type_id in lane.ours:
            continue
        out.append((series, hotel, room_name))
    return out


def lane_benchmark(row, lane: Lane) -> Benchmark | None:
    """:func:`benchmark_for`, with the gap, board and pairs of one lane."""
    if row is None:
        return None
    return Benchmark(hotel_id=row.id, hotel=row.name, undercut=lane.undercut,
                     with_tax=lane.with_tax, meal_plan=lane.meal_plan,
                     fallback_board=(ROOM_ONLY if lane.meal_plan else None),
                     room_pairs=dict(lane.pairs))


# -- the repricer's log, as the pages read it -------------------------
#: A rate written to RMS and read back. The one status that moved anything.
APPLIED = "applied"
#: What "changes only" shows: the writes, and the writes RMS refused. A
#: refused write is the one change an owner most needs to see, and hiding it
#: behind "All decisions" would make a failing repricer look like a quiet one.
WRITES = (APPLIED, "failed")


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """``day`` in the deployment's zone, as the UTC range it covers.

    The log is stored in UTC and read in Tamil Nadu: a change at 00:58 IST
    belongs to the 25th although it is 19:28 on the 24th in UTC.
    """
    start = datetime.combine(day, time.min, tzinfo=ZoneInfo(tz))
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def log_stmt(owner_user_id: int, *, changes_only: bool, day: date | None = None,
             tz: str = "Asia/Kolkata", limit: int = 60) -> Select:
    """The "What the repricer did" rows, newest first.

    ``changes_only`` keeps the rows that wrote a rate, or tried to and were
    refused (:data:`WRITES`). The full log is every
    30-minute decision, and the handful that wrote something were lost among
    the "unchanged" and "held" rows around them. Plain readings are never
    shown either way: they are figures a Preview took, not decisions.
    """
    stmt = select(RepricingAction).where(
        RepricingAction.owner_user_id == owner_user_id,
        RepricingAction.status.in_(WRITES) if changes_only else (RepricingAction.status != "read"),
    )
    if day is not None:
        start, end = local_day_bounds(day, tz)
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
    start, end = local_day_bounds(day, tz)
    return select(func.count()).select_from(RepricingAction).where(
        RepricingAction.owner_user_id == owner_user_id,
        RepricingAction.mode == "auto", RepricingAction.status == APPLIED,
        RepricingAction.created_at >= start, RepricingAction.created_at < end,
    )
