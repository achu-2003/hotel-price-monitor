"""Hotels, their sources, and their room types."""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select

from app.adapters.engines import Detection, detect, known_engines
from app.api.deps import AdminUser, CurrentUser, DbSession, get_object_or_404, record_audit
from app.db.models import (
    Hotel,
    HotelRecipient,
    HotelSource,
    MonitoringError,
    MonitorTarget,
    PriceChange,
    PriceObservation,
    PriceSeries,
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
    HotelPurge,
    HotelPurgeResult,
    HotelSourceCreate,
    HotelSourceOut,
    HotelSourceUpdate,
    AttachFromUrl,
    HotelUpdate,
    RoomTypeCreate,
    RoomTypeOut,
    RoomTypeUpdate,
    ReplaceUrl,
    ReplaceUrlResult,
)
from app.core.logging import get_logger
from app.services.room_matching import normalize_room_name

router = APIRouter(tags=["hotels"])
log = get_logger("api.hotels")


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
    # updated_at is maintained by the database (onupdate=now()), so after an
    # UPDATE the loaded value is stale and SQLAlchemy marks it for refresh.
    # Serialising without this raises MissingGreenlet: reading the attribute
    # would issue a query, and the async session cannot do that lazily.
    await session.refresh(hotel)
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



def _json_fragment(url: str) -> str:
    """A stable slice of an endpoint URL, for matching it again next fetch.

    Delegates to the adapter layer so that attaching a source and repairing one
    derive the identical fragment. Kept as a name here because this module's
    callers read better for it.
    """
    from app.adapters.discovery import json_fragment

    return json_fragment(url)


def _detection_for_discovery(url: str):
    """Wrap a discovered site in the same shape a known engine produces.

    A generic profile, so everything downstream — the source row, the URL
    template, the placeholders — behaves identically whether the config came
    from a hand-written profile or from inspecting the page.
    """
    from urllib.parse import urlparse

    from app.adapters.engines import Detection, EngineProfile, parameterise_url

    host = (urlparse(url).hostname or "site").lower()
    template, substituted = parameterise_url(url)
    profile = EngineProfile(
        key=host.replace(".", "-")[:60],
        display_name=f"{host} (auto-discovered)",
        adapter_key="playwright_direct_site",
        domains=(host,),
        adapter_config={},
        notes="Configuration derived by inspecting the page, then verified "
              "against the prices it displays.",
    )
    return Detection(profile=profile, url_template=template,
                     external_id=None, substituted=substituted)


@router.post(
    "/hotels/{hotel_id}/sources/from-url",
    response_model=HotelSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def attach_source_from_url(
    hotel_id: int,
    payload: AttachFromUrl,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Attach a hotel by pasting its booking URL. Everything else is derived.

    The URL already states which engine a hotel is on, and the engine
    determines the adapter, the field mapping and where the property code
    lives. Making an operator restate all three was busywork with three
    chances to get it wrong.

    A 422 for an unrecognised engine is the honest answer: this deliberately
    will not invent a configuration for a site nobody has inspected. Run
    scripts/probe_site.py, add a profile to app/adapters/engines.py, and every
    hotel on that engine becomes a paste from then on.
    """
    await get_object_or_404(session, Hotel, hotel_id, "Hotel")

    detection = detect(str(payload.url))

    discovered_config: dict | None = None
    discovery_note: str | None = None
    if detection is None:
        # Unknown engine. Rather than refuse, inspect the page: load it, watch
        # what JSON it fetches, and look for a room list whose prices also
        # appear on screen. Slow (a browser, ~30-60s) but it happens once per
        # hotel, and it is the difference between "we support your site" and
        # "someone must write a profile first".
        #
        # Sync Playwright cannot run inside the event loop, so it goes to a
        # worker thread.
        from fastapi.concurrency import run_in_threadpool

        from app.adapters.discovery import inspect_url

        try:
            result = await run_in_threadpool(inspect_url, str(payload.url))
        except Exception as exc:  # noqa: BLE001 - reported, never leaked raw
            log.warning("discovery_failed", url=str(payload.url)[:120], error=str(exc))
            # A FetchError has already said what happened, to an
            # operator, in a sentence they can act on: which bot wall
            # matched, or how long the page was given to load. Reporting
            # only the class name threw all of that away and told
            # everyone the same thing about CAPTCHAs -- including the
            # people whose page had simply been slow.
            from app.core.errors import FetchError

            if isinstance(exc, FetchError):
                detail = f"Could not inspect that page. {exc}"
            else:
                detail = (
                    f"Could not inspect that page: {type(exc).__name__}. If it "
                    f"showed a CAPTCHA or a bot wall, that is a refusal and this "
                    f"hotel needs manual entry instead."
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail,
            ) from exc

        if not result.ok:
            reason = result.notes[-1] if result.notes else "nothing usable was found."
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Inspected the page but could not find prices it could trust. "
                    f"{reason} This usually means the rates only load after "
                    f"choosing dates inside the page. Try the URL you land on "
                    f"AFTER clicking Book Now and picking dates — or track this "
                    f"hotel by manual entry."
                ),
            )

        best = result.best
        fragment = _json_fragment(best.source_url)
        discovered_config = best.as_adapter_config(fragment)
        discovery_note = (
            f"Auto-discovered: {best.room_count} rooms from {fragment}, "
            f"{best.corroborated}/{len(best.sample_prices)} prices confirmed "
            f"against the page."
        )
        detection = _detection_for_discovery(str(payload.url))
        log.info("source_discovered", url=str(payload.url)[:120],
                 rooms=best.room_count, fields=best.fields)
    # A URL with no date parameter cannot ask for a specific night. That is a
    # real limitation, not a reason to refuse: plenty of small hotels publish
    # one standing rate rather than pricing per night, and tracking that rate
    # is genuinely useful — it just is not the same thing.
    #
    # So it is accepted and LABELLED. The flag travels on the hotel source, and
    # the target endpoint uses it to refuse a future-dated window, which is the
    # only way this could produce a wrong number: filing today's rate under
    # next Saturday.
    standing_rate = not detection.is_complete

    profile = detection.profile

    # One source per engine, shared by every hotel on it. Created on first use
    # and reused after, which is what makes the second hotel a paste.
    source = await session.scalar(select(Source).where(Source.code == profile.key))
    if source is None:
        source = Source(
            code=profile.key,
            display_name=profile.display_name,
            adapter_key=profile.adapter_key,
            base_domain=profile.domains[0],
            rate_limit_per_min=profile.rate_limit_per_min,
            # Created disabled: a source is not fetched until a named human has
            # recorded a Terms of Service review. Auto-detection resolves the
            # technical question, never the permission one.
            is_enabled=False,
        )
        session.add(source)
        await session.flush()

    existing = await session.scalar(
        select(HotelSource).where(
            HotelSource.hotel_id == hotel_id, HotelSource.source_id == source.id
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This hotel is already attached to {source.display_name!r}.",
        )

    hotel_source = HotelSource(
        hotel_id=hotel_id,
        source_id=source.id,
        url=detection.url_template,
        external_id=detection.external_id,
        currency=payload.currency,
        adapter_config={
            **(discovered_config or profile.adapter_config),
            **({"standing_rate": True} if standing_rate else {}),
            # How this configuration was arrived at, kept next to the
            # configuration itself. A hand-written engine profile can be read
            # in the repository; a discovered one cannot be read anywhere, and
            # "3 of 3 prices confirmed against the page" is the difference
            # between a mapping someone can trust and one they have to
            # re-derive by opening the site. adapter_config is returned by this
            # endpoint, so it reaches the operator without a migration.
            **({"discovery_note": discovery_note} if discovery_note else {}),
        },
    )
    session.add(hotel_source)
    await session.flush()

    await record_audit(
        session, user=admin, action="attach_from_url", entity="hotel_source",
        entity_id=hotel_source.id,
        after={"engine": profile.key, "url_template": detection.url_template,
               "external_id": detection.external_id},
        request=request,
    )
    await session.commit()

    log.info("source_detected_from_url", hotel_id=hotel_id, engine=profile.key,
             external_id=detection.external_id, usable=source.is_usable,
             standing_rate=standing_rate)

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


def _may_carry_no_dates(detection: Detection, was_standing_rate: bool) -> bool:
    """May this replacement URL leave the stay dates out?

    A URL with no check-in parameter pins a source to whatever night the page
    happens to show. For a source that follows dates that is a silent downgrade
    -- every check re-reads one night while the dashboard goes on presenting it
    as current -- so it is refused.

    For a source that was attached from a date-less URL it is not a downgrade
    at all: it is the same kind of URL it already has. Those are STANDING RATE
    sources, accepted and labelled deliberately on attach, and the label is
    what keeps them honest (``create_target`` refuses a future-dated window on
    one). Refusing here meant a hotel could be attached from its homepage and
    then never have its link corrected, because every URL it could
    legitimately carry was rejected by the one flow that exists to fix it --
    with a staleness warning that does not describe it.
    """
    return detection.is_complete or was_standing_rate


@router.post("/hotel-sources/{hotel_source_id}/replace-url", response_model=ReplaceUrlResult)
async def replace_hotel_source_url(
    hotel_source_id: int,
    payload: ReplaceUrl,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Repoint a hotel at the correct booking page.

    WHY THIS IS NOT ``PATCH /hotel-sources/{id}`` WITH A URL
    =======================================================
    That endpoint stores the string it is given. What is stored here is a URL
    *template*: the dates and guest counts were replaced with placeholders on
    attach so the target follows a rolling window. Pasting a fresh address-bar
    URL into the raw field would silently pin one night forever — every check
    would fetch the same date and the prices would go stale while still looking
    current. Re-detecting is the only way to correct a link safely, so it gets
    its own verb.

    WHY CHANGING THE PROPERTY NEEDS A CONFIRMATION
    ==============================================
    ``offer_key`` is built from hotel, source, room, dates and occupancy — not
    from the URL. Prices fetched from a different property therefore land in
    the SAME series as the old ones, and the comparison state machine reads the
    gap between two hotels as a price change: a confirmed alert saying a room
    moved from 1,850 to 4,200 when nothing moved at all.

    The fix is to drop the baselines, not the history. ``price_series`` rows
    hold "the last price we saw", so deleting them makes the next check a first
    sighting — recorded, and told to nobody. ``price_observations`` are left
    alone: they are what was genuinely on the screen at the time, and they are
    the answer to "why does last week look like a different hotel?"
    """
    hotel_source = await get_object_or_404(
        session, HotelSource, hotel_source_id, "Hotel source"
    )

    current_source = await session.get(Source, hotel_source.source_id)

    detection = detect(payload.url)
    if detection is None:
        # No engine profile, which for an AUTO-DISCOVERED source is normal
        # rather than a problem: it was attached by inspecting the page, and
        # no profile was ever written for it.
        #
        # Refusing here left those sources with no way to correct their link
        # from the dashboard at all -- and they are the ones most likely to
        # need it, because a URL that no profile understands is exactly the
        # kind this function's own docstring is about. One swiftbook hotel was
        # stored with its dates baked in, and nothing short of deleting the
        # source and losing its history could change that.
        #
        # Safe without inspecting the page because this endpoint changes only
        # the URL: no selector is re-derived, so nothing is being invented for
        # a site nobody has looked at. The same-domain condition is what keeps
        # it honest -- re-pointing a source at a page on the host it was
        # already reading is a new link, not a new engine.
        replacement_host = (urlparse(payload.url).hostname or "").lower()
        if (
            current_source is not None
            and current_source.base_domain
            and current_source.base_domain.lower() in replacement_host
        ):
            detection = _detection_for_discovery(payload.url)
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"That URL is not on a booking engine this build knows, and "
                    f"it is not on {current_source.base_domain if current_source else 'this source'}'s "
                    f"domain either. Known: "
                    f"{[e['display_name'] for e in known_engines()]}. Run "
                    f"scripts/probe_site.py against it to find out what it exposes."
                ),
            )
    # A SOURCE ATTACHED FROM A DATE-LESS URL IS ALREADY A STANDING RATE.
    #
    # attach_source_from_url does not refuse those: it accepts and LABELS them
    # (see the comment above ``standing_rate = not detection.is_complete``),
    # because a site that publishes one current price with no notion of a night
    # is a real thing to monitor, and the flag is what stops a future-dated
    # target filing today's rate under next Saturday.
    #
    # This guard did not know that. So a hotel could be attached from its
    # homepage and then never have its link corrected -- every URL that source
    # could legitimately carry was refused by the one flow that exists to fix
    # it, with a warning about going stale that does not apply to it. It is the
    # same trap the unknown-engine branch above was fixed for, reached by a
    # different door.
    #
    # Refused only when it would DOWNGRADE: a source that follows dates today
    # must not be quietly pinned to a single night.
    was_standing_rate = bool((hotel_source.adapter_config or {}).get("standing_rate"))
    if not _may_carry_no_dates(detection, was_standing_rate):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No check-in date parameter was found in that URL, so every "
                "check would fetch the same night forever and the prices would "
                "silently go stale. Paste the URL from a page showing rates for "
                "specific dates."
            ),
        )

    # Identity is the DOMAIN, not the source's code. A source created by hand
    # before the paste flow existed can carry any code at all, and rejecting a
    # perfectly correct URL because a label does not match would be a puzzle
    # with no way out from the dashboard. The adapter has to agree too: the
    # stored one is what will fetch the new URL.
    host = (urlparse(payload.url).hostname or "").lower()
    same_engine = (
        current_source is not None
        and current_source.base_domain
        and current_source.base_domain.lower() in host
        and current_source.adapter_key == detection.profile.adapter_key
    )
    if not same_engine:
        # A different engine means a different source, a different rate-limit
        # budget and a different Terms of Service review. Moving the row would
        # inherit an approval that was never given for this site.
        current_name = current_source.display_name if current_source else "another engine"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That URL is on {detection.profile.display_name}, but this link "
                f"is attached to {current_name}. Attach it as a separate source "
                "instead — each engine has its own Terms of Service review and "
                "its own price history."
            ),
        )

    property_changed = detection.external_id != hotel_source.external_id
    if property_changed and not payload.discard_history:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That URL is a different property ({hotel_source.external_id or 'unknown'}"
                f" → {detection.external_id or 'unknown'}). Prices already "
                "collected belong to the old one, and comparing them against the "
                "new property would report the difference between two hotels as "
                "a price change. Confirm to drop the stored baselines and start "
                "fresh — the raw observations are kept."
            ),
        )

    before = {"url": hotel_source.url, "external_id": hotel_source.external_id}

    series_reset = 0
    if property_changed:
        result = await session.execute(
            delete(PriceSeries).where(
                PriceSeries.hotel_id == hotel_source.hotel_id,
                PriceSeries.source_id == hotel_source.source_id,
            )
        )
        series_reset = result.rowcount or 0

    hotel_source.url = detection.url_template
    hotel_source.external_id = detection.external_id

    # The rest of adapter_config is deliberately left alone: the engine has not
    # changed, so the profile defaults are the ones already stored, and
    # overwriting would throw away any selector an operator hand-fixed during
    # an outage.
    #
    # url_template is the exception, and it is not optional. The direct-site
    # adapter reads `config.get("url_template") or context.url`, so a stale
    # template there would keep fetching the OLD property while the row, the
    # dashboard and the audit trail all said the link had been replaced —
    # the worst kind of wrong, because everything visible agrees.
    config = dict(hotel_source.adapter_config or {})
    if "url_template" in config:
        config["url_template"] = detection.url_template

    # A DATED URL ON A STANDING-RATE SOURCE IS AN UPGRADE, and the flag has to
    # go with it. It is what makes create_target refuse a future-dated window,
    # so leaving it behind would keep the hotel pinned to tonight by a fact
    # that had just stopped being true -- and the operator who fixed the link
    # would have no way to see why.
    if was_standing_rate and detection.is_complete:
        config.pop("standing_rate", None)
        log.info("standing_rate_cleared", hotel_source_id=hotel_source_id)

    if config != (hotel_source.adapter_config or {}):
        hotel_source.adapter_config = config

    hotel_source.last_verified_at = None

    await record_audit(
        session, user=admin, action="replace_url", entity="hotel_source",
        entity_id=hotel_source_id, before=before,
        after={"url": detection.url_template, "external_id": detection.external_id,
               "property_changed": property_changed, "series_reset": series_reset},
        request=request,
    )
    await session.commit()

    log.info(
        "hotel_source_url_replaced",
        hotel_source_id=hotel_source_id,
        engine=detection.profile.key,
        property_changed=property_changed,
        series_reset=series_reset,
    )

    return ReplaceUrlResult(
        hotel_source=HotelSourceOut(
            **{k: getattr(hotel_source, k) for k in
               ("id", "hotel_id", "source_id", "url", "external_id", "currency",
                "adapter_config", "is_active", "last_verified_at")},
            source_code=current_source.code,
            adapter_key=current_source.adapter_key,
        ),
        property_changed=property_changed,
        series_reset=series_reset,
    )


@router.post("/hotels/{hotel_id}/purge", response_model=HotelPurgeResult)
async def purge_hotel(
    hotel_id: int,
    payload: HotelPurge,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Erase a hotel and everything ever collected for it. Not reversible.

    WHY THIS IS SEPARATE FROM ``DELETE /hotels/{id}``
    ================================================
    That one deactivates: it stops the checks and keeps the history, because
    history is the product and a competitor you stopped watching in March is
    still the answer to "what did they charge last March?".

    This exists for the other case, which is just as real: a hotel added by
    mistake, or attached to the wrong property, whose entire recorded history
    is noise. Leaving those in the list forever teaches people to ignore the
    list.

    THE NAME MUST BE TYPED
    ======================
    Not a checkbox and not a confirm dialog. This destroys data no backup
    inside the application can return, and the cost of a mis-click is
    permanent, so the confirmation is made to cost a deliberate ten seconds.

    OBSERVATIONS ARE DELETED BY KEY, NOT BY CASCADE
    ===============================================
    ``price_observations`` has no ``hotel_id`` — it is keyed by ``offer_key``,
    which is what makes the table cheap to write. So the cascade cannot reach
    it, and skipping this step would leave rows that no query can ever return
    and no retention policy will ever prune.
    """
    hotel = await get_object_or_404(session, Hotel, hotel_id, "Hotel")

    if payload.confirm_name.strip() != hotel.name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Type the hotel's name exactly to confirm. Expected "
                f"{hotel.name!r}."
            ),
        )

    offer_keys = list(
        (
            await session.scalars(
                select(PriceSeries.offer_key).where(PriceSeries.hotel_id == hotel_id)
            )
        ).all()
    )

    observations = 0
    if offer_keys:
        result = await session.execute(
            delete(PriceObservation).where(PriceObservation.offer_key.in_(offer_keys))
        )
        observations = result.rowcount or 0

    changes = await session.scalar(
        select(func.count(PriceChange.id)).where(PriceChange.hotel_id == hotel_id)
    ) or 0

    # Recorded BEFORE the row goes, so the audit trail keeps the name and the
    # numbers after there is nothing left to look up.
    await record_audit(
        session, user=admin, action="purge", entity="hotel", entity_id=hotel_id,
        before={"name": hotel.name, "slug": hotel.slug},
        after={"series": len(offer_keys), "observations": observations,
               "changes": changes},
        request=request,
    )

    # A Core DELETE, not session.delete(): the ORM cascade would lazy-load
    # every relationship to walk it, which an async session cannot do
    # implicitly. The database's own ON DELETE CASCADE covers sources, rooms,
    # targets, series, changes, errors and recipient links.
    await session.execute(delete(Hotel).where(Hotel.id == hotel_id))
    await session.commit()

    log.info(
        "hotel_purged", hotel_id=hotel_id, name=hotel.name,
        series=len(offer_keys), observations=observations, changes=changes,
    )

    return HotelPurgeResult(
        hotel_id=hotel_id,
        name=hotel.name,
        series_deleted=len(offer_keys),
        observations_deleted=observations,
        changes_deleted=changes,
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
