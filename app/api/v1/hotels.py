"""Hotels, their sources, and their room types."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, DbSession, get_object_or_404, record_audit
from app.db.models import (
    Hotel,
    HotelRecipient,
    HotelSource,
    MonitoringError,
    MonitorTarget,
    Recipient,
    RoomType,
    RoomTypeAlias,
    Source,
    UnmatchedOffer,
)
from app.db.models.enums import MatchMethod
from app.schemas.common import Page
from app.schemas.hotels import (
    AliasCreate,
    AliasOut,
    HotelCreate,
    HotelDetail,
    HotelHealth,
    HotelOut,
    HotelSourceCreate,
    HotelSourceOut,
    HotelSourceUpdate,
    HotelUpdate,
    RoomTypeCreate,
    RoomTypeOut,
    RoomTypeUpdate,
)
from app.services.room_matching import normalize_room_name

router = APIRouter(tags=["hotels"])


def _slugify(name: str) -> str:
    cleaned = "-".join(name.lower().strip().split())
    return "".join(c for c in cleaned if c.isalnum() or c == "-")[:200] or "hotel"


@router.get("/hotels", response_model=Page[HotelOut])
async def list_hotels(
    session: DbSession,
    _user: CurrentUser,
    active: bool | None = None,
    q: str | None = Query(default=None, description="Case-insensitive name search"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Offset pagination is fine here and only here.

    There are thirty hotels, not thirty million, and an operator wants a
    stable page 2. The price endpoints use cursors because those tables grow
    without limit.
    """
    statement = select(Hotel)
    if active is not None:
        statement = statement.where(Hotel.is_active.is_(active))
    if q:
        statement = statement.where(Hotel.name.ilike(f"%{q}%"))

    total = await session.scalar(
        select(func.count()).select_from(statement.subquery())
    )
    rows = (
        await session.scalars(
            statement.order_by(Hotel.name).limit(limit).offset(offset)
        )
    ).all()
    return Page[HotelOut](items=[HotelOut.model_validate(h) for h in rows], total=total)


@router.post("/hotels", response_model=HotelOut, status_code=status.HTTP_201_CREATED)
async def create_hotel(
    payload: HotelCreate, request: Request, session: DbSession, admin: AdminUser
):
    slug = payload.slug or _slugify(payload.name)
    if await session.scalar(select(Hotel).where(Hotel.slug == slug)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A hotel with slug {slug!r} already exists.",
        )

    hotel = Hotel(**payload.model_dump(exclude={"slug"}), slug=slug)
    session.add(hotel)
    await session.flush()
    await record_audit(
        session, user=admin, action="create", entity="hotel", entity_id=hotel.id,
        after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    return HotelOut.model_validate(hotel)


@router.get("/hotels/{hotel_id}", response_model=HotelDetail)
async def get_hotel(hotel_id: int, session: DbSession, _user: CurrentUser):
    hotel = await get_object_or_404(session, Hotel, hotel_id, "Hotel")

    sources = (
        await session.execute(
            select(HotelSource, Source)
            .join(Source, HotelSource.source_id == Source.id)
            .where(HotelSource.hotel_id == hotel_id)
        )
    ).all()
    rooms = (
        await session.scalars(
            select(RoomType).where(RoomType.hotel_id == hotel_id).order_by(RoomType.sort_order)
        )
    ).all()
    recipients = (
        await session.execute(
            select(HotelRecipient, Recipient)
            .join(Recipient, HotelRecipient.recipient_id == Recipient.id)
            .where(HotelRecipient.hotel_id == hotel_id)
        )
    ).all()

    detail = HotelDetail.model_validate(hotel)
    detail.sources = [
        HotelSourceOut(
            **{k: getattr(hs, k) for k in
               ("id", "hotel_id", "source_id", "url", "external_id", "currency",
                "adapter_config", "is_active", "last_verified_at")},
            source_code=src.code,
            adapter_key=src.adapter_key,
        )
        for hs, src in sources
    ]
    detail.room_types = [RoomTypeOut.model_validate(r) for r in rooms]
    detail.recipients = [
        {
            "recipient_id": rec.id,
            "name": rec.name,
            "channels": link.channels,
            "is_active": link.is_active,
        }
        for link, rec in recipients
    ]
    detail.health = await _hotel_health(session, hotel_id)
    return detail


async def _hotel_health(session, hotel_id: int) -> HotelHealth:
    """The Health tab's per-hotel summary.

    ``is_stale`` is computed in Python from ``MonitorTarget.is_stale`` so the
    definition of "stale" lives in exactly one place — a duplicated SQL
    version of it would drift the first time the rule changed.
    """
    now = datetime.now(UTC)
    targets = (
        await session.scalars(
            select(MonitorTarget)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .where(HotelSource.hotel_id == hotel_id)
        )
    ).all()

    errors = await session.scalar(
        select(func.count(MonitoringError.id)).where(
            MonitoringError.hotel_id == hotel_id, MonitoringError.resolved_at.is_(None)
        )
    )
    unmatched = await session.scalar(
        select(func.count(UnmatchedOffer.id))
        .join(HotelSource, UnmatchedOffer.hotel_source_id == HotelSource.id)
        .where(HotelSource.hotel_id == hotel_id, UnmatchedOffer.resolved_at.is_(None))
    )

    successes = [t.last_success_at for t in targets if t.last_success_at]
    enabled = [t for t in targets if t.is_enabled]
    return HotelHealth(
        targets_total=len(targets),
        targets_enabled=len(enabled),
        circuits_open=sum(1 for t in targets if t.circuit_state.value == "open"),
        last_success_at=max(successes) if successes else None,
        unresolved_errors=errors or 0,
        unmatched_rooms=unmatched or 0,
        is_stale=any(t.is_stale(now) for t in enabled),
    )


@router.patch("/hotels/{hotel_id}", response_model=HotelOut)
async def update_hotel(
    hotel_id: int, payload: HotelUpdate, request: Request, session: DbSession, admin: AdminUser
):
    hotel = await get_object_or_404(session, Hotel, hotel_id, "Hotel")
    before = {c.name: getattr(hotel, c.name) for c in hotel.__table__.columns}

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(hotel, field, value)

    await record_audit(
        session, user=admin, action="update", entity="hotel", entity_id=hotel_id,
        before=before, after=payload.model_dump(mode="json", exclude_unset=True),
        request=request,
    )
    await session.commit()
    return HotelOut.model_validate(hotel)


@router.delete("/hotels/{hotel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_hotel(
    hotel_id: int, request: Request, session: DbSession, admin: AdminUser
):
    """Soft delete.

    A hard delete would cascade through ``price_series`` and take the price
    history with it. History is the product; deactivating stops the checks and
    keeps everything that was already learned.
    """
    hotel = await get_object_or_404(session, Hotel, hotel_id, "Hotel")
    hotel.is_active = False

    targets = (
        await session.scalars(
            select(MonitorTarget)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .where(HotelSource.hotel_id == hotel_id)
        )
    ).all()
    for target in targets:
        target.is_enabled = False

    await record_audit(
        session, user=admin, action="deactivate", entity="hotel",
        entity_id=hotel_id, request=request,
    )
    await session.commit()


# -- sources attached to a hotel -------------------------------------
@router.post(
    "/hotels/{hotel_id}/sources",
    response_model=HotelSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def attach_source(
    hotel_id: int,
    payload: HotelSourceCreate,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    await get_object_or_404(session, Hotel, hotel_id, "Hotel")
    source = await get_object_or_404(session, Source, payload.source_id, "Source")

    existing = await session.scalar(
        select(HotelSource).where(
            HotelSource.hotel_id == hotel_id, HotelSource.source_id == payload.source_id
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This hotel is already attached to source {source.code!r}.",
        )

    hotel_source = HotelSource(hotel_id=hotel_id, **payload.model_dump())
    session.add(hotel_source)
    await session.flush()
    await record_audit(
        session, user=admin, action="create", entity="hotel_source",
        entity_id=hotel_source.id, after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()

    return HotelSourceOut(
        **{k: getattr(hotel_source, k) for k in
           ("id", "hotel_id", "source_id", "url", "external_id", "currency",
            "adapter_config", "is_active", "last_verified_at")},
        source_code=source.code,
        adapter_key=source.adapter_key,
    )


@router.patch("/hotel-sources/{hotel_source_id}", response_model=HotelSourceOut)
async def update_hotel_source(
    hotel_source_id: int,
    payload: HotelSourceUpdate,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Editing ``adapter_config`` here is how a broken selector gets fixed.

    That is the whole point of keeping it in the database: a site redesign at
    5 PM is a config edit and the next scheduled check picks it up, rather
    than a code change, a build, and a deploy.
    """
    hotel_source = await get_object_or_404(
        session, HotelSource, hotel_source_id, "Hotel source"
    )
    before = {"adapter_config": hotel_source.adapter_config, "url": hotel_source.url}

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(hotel_source, field, value)

    source = await session.get(Source, hotel_source.source_id)
    await record_audit(
        session, user=admin, action="update", entity="hotel_source",
        entity_id=hotel_source_id, before=before,
        after=payload.model_dump(mode="json", exclude_unset=True), request=request,
    )
    await session.commit()

    return HotelSourceOut(
        **{k: getattr(hotel_source, k) for k in
           ("id", "hotel_id", "source_id", "url", "external_id", "currency",
            "adapter_config", "is_active", "last_verified_at")},
        source_code=source.code if source else None,
        adapter_key=source.adapter_key if source else None,
    )


# -- room types ------------------------------------------------------
@router.get("/hotels/{hotel_id}/rooms", response_model=list[RoomTypeOut])
async def list_rooms(hotel_id: int, session: DbSession, _user: CurrentUser):
    rooms = (
        await session.scalars(
            select(RoomType)
            .where(RoomType.hotel_id == hotel_id)
            .order_by(RoomType.sort_order, RoomType.name)
        )
    ).all()
    return [RoomTypeOut.model_validate(r) for r in rooms]


@router.post(
    "/hotels/{hotel_id}/rooms", response_model=RoomTypeOut, status_code=status.HTTP_201_CREATED
)
async def create_room(
    hotel_id: int,
    payload: RoomTypeCreate,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    await get_object_or_404(session, Hotel, hotel_id, "Hotel")
    canonical = normalize_room_name(payload.name)
    if not canonical:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{payload.name!r} normalises to nothing and could never be "
                   f"matched against a site's room name.",
        )

    existing = await session.scalar(
        select(RoomType).where(
            RoomType.hotel_id == hotel_id, RoomType.canonical_name == canonical
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{existing.name!r} already covers this room "
                   f"(both normalise to {canonical!r}).",
        )

    room = RoomType(hotel_id=hotel_id, canonical_name=canonical, **payload.model_dump())
    session.add(room)
    await session.flush()
    await record_audit(
        session, user=admin, action="create", entity="room_type", entity_id=room.id,
        after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    return RoomTypeOut.model_validate(room)


@router.patch("/rooms/{room_id}", response_model=RoomTypeOut)
async def update_room(
    room_id: int, payload: RoomTypeUpdate, request: Request, session: DbSession, admin: AdminUser
):
    room = await get_object_or_404(session, RoomType, room_id, "Room type")
    data = payload.model_dump(exclude_unset=True)

    for field, value in data.items():
        setattr(room, field, value)
    if "name" in data:
        # Renaming for display must not silently re-key the matching, so the
        # canonical form is recomputed and the existing aliases keep pointing
        # at this same room id.
        room.canonical_name = normalize_room_name(room.name) or room.canonical_name

    await record_audit(
        session, user=admin, action="update", entity="room_type", entity_id=room_id,
        after=data, request=request,
    )
    await session.commit()
    return RoomTypeOut.model_validate(room)


@router.post(
    "/rooms/{room_id}/aliases", response_model=AliasOut, status_code=status.HTTP_201_CREATED
)
async def create_alias(
    room_id: int, payload: AliasCreate, request: Request, session: DbSession, admin: AdminUser
):
    """Teach the matcher that a site's name means this room.

    Recorded as ``manual``, which outranks fuzzy matching from then on: a
    human decision is the highest-trust signal available, and it is what stops
    the same unmatched room reappearing every thirty minutes.
    """
    room = await get_object_or_404(session, RoomType, room_id, "Room type")
    normalized = normalize_room_name(payload.raw_name)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{payload.raw_name!r} normalises to nothing and cannot be an alias.",
        )

    existing = await session.scalar(
        select(RoomTypeAlias).where(
            RoomTypeAlias.source_id == payload.source_id,
            RoomTypeAlias.hotel_id == room.hotel_id,
            RoomTypeAlias.normalized_name == normalized,
        )
    )
    if existing is not None:
        # Re-pointing an existing alias is a legitimate correction — it is how
        # a bad automatic match gets fixed — so update rather than refuse.
        existing.room_type_id = room_id
        existing.match_method = MatchMethod.MANUAL
        existing.confidence = 1.0
        existing.raw_name = payload.raw_name[:300]
        await session.commit()
        return AliasOut.model_validate(existing)

    alias = RoomTypeAlias(
        room_type_id=room_id,
        hotel_id=room.hotel_id,
        source_id=payload.source_id,
        raw_name=payload.raw_name[:300],
        normalized_name=normalized,
        match_method=MatchMethod.MANUAL,
        confidence=1.0,
    )
    session.add(alias)
    await session.flush()
    await record_audit(
        session, user=admin, action="create", entity="room_type_alias",
        entity_id=alias.id, after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    return AliasOut.model_validate(alias)
