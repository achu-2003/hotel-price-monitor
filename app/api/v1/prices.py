"""Reading prices: current, history, changes, the matrix, and the unmatched queue.

All read-only, all available to viewers as well as admins — except resolving
an unmatched room, which is a configuration change and requires an admin.

Everything current comes from ``price_series`` and never from the observation
table. Answering "what is the price now?" with ``ORDER BY checked_at DESC
LIMIT 1`` over a forever-growing partitioned table, once per room per hotel,
is how a dashboard that was fast in week one becomes unusable in month six.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import (
    AdminUser, CurrentUser, DbSession, get_object_or_404, owned_hotel_or_404, record_audit,
)
from app.services.ownership import owns, scope_hotels
from app.db.models import (
    ChangeDirection,
    Hotel,
    HotelSource,
    PriceChange,
    PriceObservation,
    PriceSeries,
    RoomType,
    RoomTypeAlias,
    UnmatchedOffer,
)
from app.db.models.enums import MatchMethod
from app.schemas.common import Page
from app.schemas.prices import (
    CurrentPriceOut,
    HistoryOut,
    HistoryPoint,
    MatrixCell,
    MatrixOut,
    MatrixRow,
    PriceChangeOut,
    ResolveUnmatchedIn,
    UnmatchedOfferOut,
)

router = APIRouter(prefix="/prices", tags=["prices"])

#: A series unchecked for this long is shown as stale rather than current.
#: Deliberately generous — it is a "something is wrong" marker, not an SLA.
_STALE_AFTER = timedelta(hours=3)


@router.get("/current", response_model=Page[CurrentPriceOut])
async def current_prices(
    session: DbSession,
    user: CurrentUser,
    hotel_id: int | None = None,
    check_in: date | None = None,
    check_out: date | None = None,
    available_only: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    statement = scope_hotels(
        select(PriceSeries, Hotel.name, RoomType.name)
        .join(Hotel, PriceSeries.hotel_id == Hotel.id)
        .join(RoomType, PriceSeries.room_type_id == RoomType.id),
        user,
    )
    if hotel_id is not None:
        statement = statement.where(PriceSeries.hotel_id == hotel_id)
    if check_in is not None:
        statement = statement.where(PriceSeries.check_in == check_in)
    if check_out is not None:
        statement = statement.where(PriceSeries.check_out == check_out)
    if available_only:
        statement = statement.where(PriceSeries.is_available.is_(True))

    rows = (
        await session.execute(
            statement.order_by(Hotel.name, RoomType.sort_order, PriceSeries.check_in)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    now = datetime.now(UTC)
    items = []
    for series, hotel_name, room_name in rows:
        out = CurrentPriceOut.model_validate(series)
        out.hotel_name = hotel_name
        out.room_name = room_name
        out.is_stale = (now - series.last_checked_at) > _STALE_AFTER
        items.append(out)
    return Page[CurrentPriceOut](items=items)


@router.get("/history", response_model=HistoryOut)
async def price_history(
    session: DbSession,
    user: CurrentUser,
    offer_key: str = Query(min_length=64, max_length=64),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    bucket: str = Query(default="raw", pattern="^(raw|hourly|daily)$"),
    limit: int = Query(default=2000, ge=1, le=10000),
):
    """The price chart for one offer.

    Bucketing is done in SQL rather than in Python: a year of half-hourly
    observations is ~17,000 rows, and sending those to a browser to draw a
    500-pixel-wide chart wastes bandwidth on both ends.

    ``date_trunc`` keeps the aggregation on an indexed range scan of the
    partition, so the query cost tracks the window asked for, not the table.
    """
    series = await session.get(PriceSeries, offer_key)
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No price series with that offer key.",
        )

    # An offer key is a hash, not a guessable id — but "hard to guess" is not
    # an access rule, and one leaked key from a shared screenshot should not
    # hand over a competitor's whole price history.
    hotel = await session.get(Hotel, series.hotel_id)
    if not owns(hotel, user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No price series with that offer key.",
        )

    to = to or datetime.now(UTC)
    from_ = from_ or (to - timedelta(days=30))

    if bucket == "raw":
        rows = (
            await session.execute(
                select(
                    PriceObservation.checked_at,
                    PriceObservation.price_inclusive,
                    PriceObservation.price_exclusive,
                    PriceObservation.is_available,
                    PriceObservation.rooms_left,
                )
                .where(
                    PriceObservation.offer_key == offer_key,
                    PriceObservation.checked_at >= from_,
                    PriceObservation.checked_at <= to,
                )
                .order_by(PriceObservation.checked_at)
                .limit(limit)
            )
        ).all()
        points = [
            HistoryPoint(
                checked_at=checked_at,
                price_inclusive=inclusive,
                price_exclusive=exclusive,
                is_available=available,
                rooms_left=rooms_left,
            )
            for checked_at, inclusive, exclusive, available, rooms_left in rows
        ]
    else:
        truncation = "hour" if bucket == "hourly" else "day"
        bucket_col = func.date_trunc(truncation, PriceObservation.checked_at).label("bucket")
        rows = (
            await session.execute(
                select(
                    bucket_col,
                    # The average within a bucket, not the last value: a bucket
                    # containing a change should show the transition rather than
                    # hiding it behind whichever observation happened to be last.
                    func.avg(PriceObservation.price_inclusive),
                    func.avg(PriceObservation.price_exclusive),
                    func.bool_or(PriceObservation.is_available),
                )
                .where(
                    PriceObservation.offer_key == offer_key,
                    PriceObservation.checked_at >= from_,
                    PriceObservation.checked_at <= to,
                )
                .group_by(bucket_col)
                .order_by(bucket_col)
                .limit(limit)
            )
        ).all()
        points = [
            HistoryPoint(
                checked_at=bucket_at,
                price_inclusive=inclusive,
                price_exclusive=exclusive,
                is_available=bool(available),
            )
            for bucket_at, inclusive, exclusive, available in rows
        ]

    room = await session.get(RoomType, series.room_type_id)
    return HistoryOut(
        offer_key=offer_key,
        hotel_name=hotel.name if hotel else None,
        room_name=room.name if room else None,
        check_in=series.check_in,
        check_out=series.check_out,
        currency=series.currency,
        bucket=bucket,
        points=points,
    )


@router.get("/changes", response_model=Page[PriceChangeOut])
async def list_changes(
    session: DbSession,
    user: CurrentUser,
    hotel_id: int | None = None,
    direction: ChangeDirection | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """The changes feed — the answer to "what happened today?"."""
    statement = scope_hotels(
        select(PriceChange, Hotel.name, RoomType.name, PriceSeries.check_in,
               PriceSeries.check_out)
        .join(Hotel, PriceChange.hotel_id == Hotel.id)
        .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
        .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id),
        user,
    )
    if hotel_id is not None:
        statement = statement.where(PriceChange.hotel_id == hotel_id)
    if direction is not None:
        statement = statement.where(PriceChange.direction == direction)
    if from_ is not None:
        statement = statement.where(PriceChange.changed_at >= from_)
    if to is not None:
        statement = statement.where(PriceChange.changed_at <= to)

    rows = (
        await session.execute(
            statement.order_by(PriceChange.changed_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    items = []
    for change, hotel_name, room_name, ci, co in rows:
        out = PriceChangeOut.model_validate(change)
        out.hotel_name = hotel_name
        out.room_name = room_name
        out.check_in = ci
        out.check_out = co
        items.append(out)
    return Page[PriceChangeOut](items=items)


@router.get("/matrix", response_model=MatrixOut)
async def price_matrix(
    session: DbSession,
    user: CurrentUser,
    check_in: date,
    check_out: date,
    adults: int = Query(default=2, ge=1, le=20),
    children: int = Query(default=0, ge=0, le=20),
):
    """Every hotel x room for one stay window: the screen this system exists for.

    Occupancy is a required part of the query, not an afterthought. A 2-guest
    rate and a 3-guest rate are different offers, and a matrix that mixed them
    would look like a price comparison while comparing nothing.
    """
    rows = (
        await session.execute(
            scope_hotels(
                select(PriceSeries, Hotel, RoomType.name)
                .join(Hotel, PriceSeries.hotel_id == Hotel.id)
                .join(RoomType, PriceSeries.room_type_id == RoomType.id)
                .where(
                    PriceSeries.check_in == check_in,
                    PriceSeries.check_out == check_out,
                    PriceSeries.adults == adults,
                    PriceSeries.children == children,
                    Hotel.is_active.is_(True),
                ),
                user,
            ).order_by(Hotel.name, RoomType.sort_order)
        )
    ).all()

    recent_cutoff = datetime.now(UTC) - timedelta(hours=24)
    grouped: dict[int, MatrixRow] = {}
    currency = "INR"

    for series, hotel, room_name in rows:
        currency = series.currency
        row = grouped.get(hotel.id)
        if row is None:
            row = MatrixRow(
                hotel_id=hotel.id,
                hotel_name=hotel.name,
                is_own_property=hotel.is_own_property,
                cells=[],
            )
            grouped[hotel.id] = row

        row.cells.append(
            MatrixCell(
                room_name=room_name,
                offer_key=series.offer_key,
                price=series.current_price,
                is_available=series.is_available,
                last_checked_at=series.last_checked_at,
                changed_recently=(
                    series.last_changed_at is not None
                    and series.last_changed_at >= recent_cutoff
                ),
            )
        )

    for row in grouped.values():
        prices = [c.price for c in row.cells if c.is_available and c.price is not None]
        row.cheapest = min(prices) if prices else None

    return MatrixOut(
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        currency=currency,
        rows=sorted(grouped.values(), key=lambda r: r.hotel_name),
        generated_at=datetime.now(UTC),
    )


@router.get("/unmatched", response_model=Page[UnmatchedOfferOut])
async def list_unmatched(
    session: DbSession,
    user: CurrentUser,
    hotel_id: int | None = None,
    include_resolved: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
):
    """Room names waiting for a human decision.

    This queue is the visible cost of never guessing. Every line here is a
    price we chose not to record rather than a series we might have corrupted,
    and clearing one costs a click and fixes it permanently.
    """
    statement = scope_hotels(
        select(UnmatchedOffer, HotelSource.hotel_id, Hotel.name, RoomType.name)
        .join(HotelSource, UnmatchedOffer.hotel_source_id == HotelSource.id)
        .join(Hotel, HotelSource.hotel_id == Hotel.id)
        .outerjoin(RoomType, UnmatchedOffer.suggested_room_type_id == RoomType.id),
        user,
    )
    if not include_resolved:
        statement = statement.where(UnmatchedOffer.resolved_at.is_(None))
    if hotel_id is not None:
        statement = statement.where(HotelSource.hotel_id == hotel_id)

    rows = (
        await session.execute(
            statement.order_by(UnmatchedOffer.occurrence_count.desc()).limit(limit)
        )
    ).all()

    items = []
    for unmatched, hid, hotel_name, suggested_name in rows:
        out = UnmatchedOfferOut.model_validate(unmatched)
        out.hotel_id = hid
        out.hotel_name = hotel_name
        out.suggested_room_name = suggested_name
        items.append(out)
    return Page[UnmatchedOfferOut](items=items)


@router.post("/unmatched/{unmatched_id}/dismiss", response_model=UnmatchedOfferOut)
async def dismiss_unmatched(
    unmatched_id: int,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Close the row without mapping it to anything. "None of these."

    WHY THIS HAS TO EXIST
    =====================
    The queue asks "which of this hotel's rooms is this?", and until now the
    only way to answer was to pick one. That is fine while the question is
    fair. It stops being fair the moment the name in the queue came from a
    BROKEN selector:

        name on the site : Room        (30x)
        best guess       : no candidate
        map to           : Villa

    "Room" is a category chip the scan mistook for a room name; "Villa" is the
    only room type that hotel has, and it is the other half of the same
    mistake. Mapping one to the other writes a permanent manual alias that
    merges eleven rooms' prices into the Villa series -- the exact corruption
    the queue's own subtitle warns about, arrived at by using the queue as
    designed. With no third option, the only available action was the harmful
    one, and the honest answer -- "neither, the selector was wrong" -- could
    not be given.

    It is also how such a row ends. Once the source is repaired the site's real
    names arrive, the invented one is never seen again, and nothing clears a
    row whose name simply stopped appearing: it sits on Attention forever.

    No alias is written, so nothing is taught and nothing is merged. If the
    name turns out to be real it will be queued again the next time it is seen.
    """
    unmatched = await get_object_or_404(
        session, UnmatchedOffer, unmatched_id, "Unmatched offer"
    )
    hotel_source = await get_object_or_404(
        session, HotelSource, unmatched.hotel_source_id, "Hotel source"
    )
    await owned_hotel_or_404(session, hotel_source.hotel_id, admin)

    unmatched.resolved_at = datetime.now(UTC)
    await record_audit(
        session, user=admin, action="dismiss_unmatched", entity="unmatched_offer",
        entity_id=unmatched_id,
        after={"raw_room_name": unmatched.raw_room_name, "mapped_to": None},
        request=request,
    )
    await session.commit()
    return UnmatchedOfferOut.model_validate(unmatched)


@router.post("/unmatched/{unmatched_id}/resolve", response_model=UnmatchedOfferOut)
async def resolve_unmatched(
    unmatched_id: int,
    payload: ResolveUnmatchedIn,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Map an unknown room name to a room type, permanently.

    Writes a ``manual`` alias, which the matcher prefers over anything fuzzy
    from then on. The next check picks it up automatically — no backfill is
    attempted, because inventing history for observations we deliberately did
    not record would be worse than the gap.
    """
    unmatched = await get_object_or_404(
        session, UnmatchedOffer, unmatched_id, "Unmatched offer"
    )
    room = await get_object_or_404(session, RoomType, payload.room_type_id, "Room type")
    hotel_source = await get_object_or_404(
        session, HotelSource, unmatched.hotel_source_id, "Hotel source"
    )
    await owned_hotel_or_404(session, hotel_source.hotel_id, admin)

    if room.hotel_id != hotel_source.hotel_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Room type {room.id} belongs to hotel {room.hotel_id}, but this "
                f"unmatched offer came from hotel {hotel_source.hotel_id}. "
                f"Mapping across hotels would merge two properties' price series."
            ),
        )

    existing = await session.scalar(
        select(RoomTypeAlias).where(
            RoomTypeAlias.source_id == hotel_source.source_id,
            RoomTypeAlias.hotel_id == hotel_source.hotel_id,
            RoomTypeAlias.normalized_name == unmatched.normalized_name,
        )
    )
    if existing is not None:
        existing.room_type_id = room.id
        existing.match_method = MatchMethod.MANUAL
        existing.confidence = 1.0
    else:
        session.add(
            RoomTypeAlias(
                room_type_id=room.id,
                hotel_id=hotel_source.hotel_id,
                source_id=hotel_source.source_id,
                raw_name=unmatched.raw_room_name,
                normalized_name=unmatched.normalized_name,
                match_method=MatchMethod.MANUAL,
                confidence=1.0,
            )
        )

    unmatched.resolved_at = datetime.now(UTC)
    await record_audit(
        session, user=admin, action="resolve_unmatched", entity="unmatched_offer",
        entity_id=unmatched_id,
        after={"room_type_id": room.id, "normalized_name": unmatched.normalized_name},
        request=request,
    )
    await session.commit()
    return UnmatchedOfferOut.model_validate(unmatched)
