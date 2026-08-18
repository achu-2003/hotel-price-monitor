"""Server-rendered dashboard.

Jinja2 templates, rendered from the same async session the API uses. There is
no JavaScript framework and no build step: the whole thing is 20 screens over
a database, and a server-rendered page load is both faster to write and faster
to open than anything that would need bundling.

AUTH DIFFERS FROM THE API ON PURPOSE
====================================
The API returns 401 with a problem document. A browser hitting a page it is
not logged into should be sent to the login form instead — a raw JSON 401 in a
tab is a dead end. Same session cookie, same token, different failure mode.

MUTATIONS GO THROUGH THE API
============================
Every form here posts to ``/api/v1/...`` rather than to a parallel set of
dashboard handlers. One implementation of "create a hotel" means the
validation, the audit trail and the authorisation rules cannot drift between
the two entry points.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import jwt
from fastapi import APIRouter, Cookie, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.api.deps import SESSION_COOKIE, DbSession
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db.models import (
    CheckRun,
    CircuitState,
    Hotel,
    HotelSource,
    MonitoringError,
    MonitorTarget,
    Notification,
    PriceChange,
    PriceSeries,
    Recipient,
    RoomType,
    Source,
    UnmatchedOffer,
    User,
)
from app.notifications.render import money
from app.services.dates import local_today, next_weekend, resolve_stay_window

router = APIRouter(include_in_schema=False)
log = get_logger("dashboard")

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# Available in every template: prices are shown in Indian grouping everywhere,
# and reimplementing that in Jinja would be a second place to get it wrong.
templates.env.globals["money"] = money
templates.env.globals["now"] = lambda: datetime.now(UTC)


class NotLoggedIn(Exception):
    """Raised by the dependency, converted to a redirect by the route."""


async def dashboard_user(
    session: DbSession,
    hpm_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User | None:
    """Resolve the logged-in user, or ``None``.

    Returns ``None`` rather than raising so each route can decide between a
    redirect and a public render — the login page itself needs the same
    dependency without being locked out by it.
    """
    if not hpm_session:
        return None
    try:
        payload = decode_token(hpm_session)
        user = await session.get(User, int(payload.get("sub", 0)))
    except (jwt.PyJWTError, TypeError, ValueError):
        return None
    return user if user and user.is_active else None


DashUser = Annotated[User | None, Depends(dashboard_user)]


def _redirect_to_login(request: Request) -> RedirectResponse:
    """Send the browser to the login form, remembering where it was going."""
    return RedirectResponse(
        url=f"/login?next={request.url.path}", status_code=303
    )


def _render(request: Request, user: User, template: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"user": user, "is_admin": user.role.value == "admin", **context},
    )


# -- login -----------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: DashUser, next: str = "/"):
    if user is not None:
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"next": next}
    )


@router.get("/logout")
async def logout_page():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, user: DashUser):
    """Where the middleware sends anyone with an outstanding password change.

    Reachable while the change is outstanding, unlike every other page — and
    the account was created with a password that is also sitting in a .env
    file, which is exactly why the change is forced.
    """
    if user is None:
        return _redirect_to_login(request)
    return templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={"user": user, "is_admin": user.role.value == "admin",
                 "forced": user.must_change_password},
    )


# -- overview --------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, user: DashUser, session: DbSession):
    """The screen someone opens first: what changed, and what is broken."""
    if user is None:
        return _redirect_to_login(request)

    now = datetime.now(UTC)
    day_ago = now - timedelta(hours=24)

    targets = (
        await session.scalars(select(MonitorTarget).where(MonitorTarget.is_enabled.is_(True)))
    ).all()

    recent_changes = (
        await session.execute(
            select(PriceChange, Hotel.name, RoomType.name)
            .join(Hotel, PriceChange.hotel_id == Hotel.id)
            .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
            .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(PriceChange.changed_at >= day_ago)
            .order_by(PriceChange.changed_at.desc())
            .limit(20)
        )
    ).all()

    failures = (
        await session.execute(
            select(MonitoringError, Hotel.name)
            .outerjoin(Hotel, MonitoringError.hotel_id == Hotel.id)
            .where(MonitoringError.resolved_at.is_(None))
            .order_by(MonitoringError.occurred_at.desc())
            .limit(10)
        )
    ).all()

    summary = {
        "hotels_active": await session.scalar(
            select(func.count(Hotel.id)).where(Hotel.is_active.is_(True))
        ),
        "targets_enabled": len(targets),
        "circuits_open": sum(1 for t in targets if t.circuit_state == CircuitState.OPEN),
        "stale_targets": sum(1 for t in targets if t.is_stale(now)),
        "changes_24h": await session.scalar(
            select(func.count(PriceChange.id)).where(PriceChange.changed_at >= day_ago)
        ),
        "unmatched": await session.scalar(
            select(func.count(UnmatchedOffer.id)).where(UnmatchedOffer.resolved_at.is_(None))
        ),
        "unresolved_errors": await session.scalar(
            select(func.count(MonitoringError.id)).where(MonitoringError.resolved_at.is_(None))
        ),
    }

    return _render(
        request, user, "overview.html",
        summary=summary,
        changes=recent_changes,
        failures=failures,
    )


# -- price matrix ----------------------------------------------------
@router.get("/matrix", response_class=HTMLResponse)
async def matrix(
    request: Request,
    user: DashUser,
    session: DbSession,
    check_in: date | None = None,
    check_out: date | None = None,
    adults: int = Query(default=2, ge=1, le=20),
):
    """All hotels x rooms for one night. The comparison screen.

    Defaults to the next weekend, because that is the window where rates move
    most and the one an operator is most often asked about.
    """
    if user is None:
        return _redirect_to_login(request)

    if check_in is None or check_out is None:
        weekend = next_weekend(local_today())
        check_in, check_out = weekend.check_in, weekend.check_out

    rows = (
        await session.execute(
            select(PriceSeries, Hotel, RoomType.name)
            .join(Hotel, PriceSeries.hotel_id == Hotel.id)
            .join(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(
                PriceSeries.check_in == check_in,
                PriceSeries.check_out == check_out,
                PriceSeries.adults == adults,
                Hotel.is_active.is_(True),
            )
            .order_by(Hotel.name, RoomType.sort_order)
        )
    ).all()

    recent_cutoff = datetime.now(UTC) - timedelta(hours=24)
    grouped: dict[int, dict] = {}
    for series, hotel, room_name in rows:
        entry = grouped.setdefault(
            hotel.id,
            {"hotel": hotel, "cells": [], "cheapest": None},
        )
        entry["cells"].append(
            {
                "room_name": room_name,
                "offer_key": series.offer_key,
                "price": series.last_price,
                "currency": series.currency,
                "is_available": series.is_available,
                "changed_recently": (
                    series.last_changed_at is not None
                    and series.last_changed_at >= recent_cutoff
                ),
                "last_checked_at": series.last_checked_at,
            }
        )

    for entry in grouped.values():
        prices = [c["price"] for c in entry["cells"] if c["is_available"] and c["price"]]
        entry["cheapest"] = min(prices) if prices else None

    return _render(
        request, user, "matrix.html",
        rows=sorted(grouped.values(), key=lambda e: e["hotel"].name),
        check_in=check_in,
        check_out=check_out,
        adults=adults,
    )


# -- hotels ----------------------------------------------------------
@router.get("/hotels", response_class=HTMLResponse)
async def hotels_page(
    request: Request, user: DashUser, session: DbSession, q: str | None = None
):
    if user is None:
        return _redirect_to_login(request)

    statement = select(Hotel).order_by(Hotel.name)
    if q:
        statement = statement.where(Hotel.name.ilike(f"%{q}%"))
    hotels = (await session.scalars(statement)).all()

    target_counts = dict(
        (
            await session.execute(
                select(HotelSource.hotel_id, func.count(MonitorTarget.id))
                .join(MonitorTarget, MonitorTarget.hotel_source_id == HotelSource.id)
                .group_by(HotelSource.hotel_id)
            )
        ).all()
    )
    return _render(
        request, user, "hotels.html", hotels=hotels, target_counts=target_counts, q=q or ""
    )


@router.get("/hotels/{hotel_id}", response_class=HTMLResponse)
async def hotel_detail(
    request: Request, hotel_id: int, user: DashUser, session: DbSession
):
    if user is None:
        return _redirect_to_login(request)

    hotel = await session.get(Hotel, hotel_id)
    if hotel is None:
        return RedirectResponse(url="/hotels", status_code=303)

    sources = (
        await session.execute(
            select(HotelSource, Source)
            .join(Source, HotelSource.source_id == Source.id)
            .where(HotelSource.hotel_id == hotel_id)
        )
    ).all()
    rooms = (
        await session.scalars(
            select(RoomType)
            .where(RoomType.hotel_id == hotel_id)
            .order_by(RoomType.sort_order, RoomType.name)
        )
    ).all()
    targets = (
        await session.scalars(
            select(MonitorTarget)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .where(HotelSource.hotel_id == hotel_id)
        )
    ).all()
    prices = (
        await session.execute(
            select(PriceSeries, RoomType.name)
            .join(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(PriceSeries.hotel_id == hotel_id)
            .order_by(PriceSeries.check_in, RoomType.sort_order)
            .limit(100)
        )
    ).all()
    runs = (
        await session.scalars(
            select(CheckRun)
            .where(CheckRun.monitor_target_id.in_([t.id for t in targets] or [0]))
            .order_by(CheckRun.started_at.desc())
            .limit(10)
        )
    ).all()

    # Every source, so the "attach a source" form can offer a dropdown. A
    # source that is not yet ToS-reviewed is still listed but flagged, because
    # attaching it is legitimate — it just will not be fetched until reviewed.
    all_sources = (await session.scalars(select(Source).order_by(Source.code))).all()
    attached_ids = {hs.source_id for hs, _ in sources}

    return _render(
        request, user, "hotel_detail.html",
        hotel=hotel, sources=sources, rooms=rooms, targets=targets,
        prices=prices, runs=runs, now=datetime.now(UTC),
        all_sources=all_sources, attached_ids=attached_ids,
        today=local_today(),
    )


# -- sources ---------------------------------------------------------
@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request, user: DashUser, session: DbSession):
    """Booking platforms, and their Terms of Service reviews.

    A source is a platform, not a hotel: a dozen properties can share one eZee
    or one OTA. Configuring the selectors once here is what makes adding the
    twelfth hotel on that platform a thirty-second job.
    """
    if user is None:
        return _redirect_to_login(request)

    rows = (await session.scalars(select(Source).order_by(Source.code))).all()
    counts = dict(
        (
            await session.execute(
                select(HotelSource.source_id, func.count(HotelSource.id))
                .group_by(HotelSource.source_id)
            )
        ).all()
    )
    from app.adapters import registry

    return _render(
        request, user, "sources.html",
        sources=rows, counts=counts, adapter_keys=registry.available_keys(),
    )


# -- changes feed ----------------------------------------------------
@router.get("/changes", response_class=HTMLResponse)
async def changes_page(
    request: Request,
    user: DashUser,
    session: DbSession,
    hotel_id: int | None = None,
    hours: int = Query(default=48, ge=1, le=720),
):
    if user is None:
        return _redirect_to_login(request)

    since = datetime.now(UTC) - timedelta(hours=hours)
    statement = (
        select(PriceChange, Hotel.name, RoomType.name, PriceSeries.check_in,
               PriceSeries.check_out)
        .join(Hotel, PriceChange.hotel_id == Hotel.id)
        .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
        .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id)
        .where(PriceChange.changed_at >= since)
    )
    if hotel_id is not None:
        statement = statement.where(PriceChange.hotel_id == hotel_id)

    rows = (
        await session.execute(
            statement.order_by(PriceChange.changed_at.desc()).limit(300)
        )
    ).all()
    hotels = (
        await session.scalars(select(Hotel).where(Hotel.is_active.is_(True)).order_by(Hotel.name))
    ).all()

    return _render(
        request, user, "changes.html",
        changes=rows, hotels=hotels, hotel_id=hotel_id, hours=hours,
    )


# -- health ----------------------------------------------------------
@router.get("/health-tab", response_class=HTMLResponse)
async def health_page(request: Request, user: DashUser, session: DbSession):
    """Errors grouped by hotel and class, plus every stale or paused target.

    Silence is listed alongside errors on purpose: a target that stopped
    checking without failing is the expensive case, and it would otherwise be
    invisible on a screen that only showed exceptions.
    """
    if user is None:
        return _redirect_to_login(request)

    now = datetime.now(UTC)
    errors = (
        await session.execute(
            select(MonitoringError, Hotel.name)
            .outerjoin(Hotel, MonitoringError.hotel_id == Hotel.id)
            .where(MonitoringError.resolved_at.is_(None))
            .order_by(MonitoringError.occurred_at.desc())
            .limit(200)
        )
    ).all()

    by_class: dict[str, int] = {}
    for error, _ in errors:
        by_class[error.error_class.value] = by_class.get(error.error_class.value, 0) + 1

    targets = (
        await session.execute(
            select(MonitorTarget, Hotel.id, Hotel.name)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .where(MonitorTarget.is_enabled.is_(True))
        )
    ).all()

    stale = [(t, hid, name) for t, hid, name in targets if t.is_stale(now)]
    paused = [
        (t, hid, name) for t, hid, name in targets
        if t.circuit_state != CircuitState.CLOSED
    ]

    return _render(
        request, user, "health.html",
        errors=errors, by_class=by_class, stale=stale, paused=paused, now=now,
    )


# -- unmatched rooms -------------------------------------------------
@router.get("/unmatched", response_class=HTMLResponse)
async def unmatched_page(request: Request, user: DashUser, session: DbSession):
    """The mapping queue: raw room names nothing resolved.

    Each line is a price deliberately not recorded rather than a series
    possibly corrupted. Clearing one takes a click and fixes it for good.
    """
    if user is None:
        return _redirect_to_login(request)

    rows = (
        await session.execute(
            select(UnmatchedOffer, Hotel.id, Hotel.name, RoomType.name)
            .join(HotelSource, UnmatchedOffer.hotel_source_id == HotelSource.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .outerjoin(RoomType, UnmatchedOffer.suggested_room_type_id == RoomType.id)
            .where(UnmatchedOffer.resolved_at.is_(None))
            .order_by(UnmatchedOffer.occurrence_count.desc())
            .limit(200)
        )
    ).all()

    hotel_ids = {hid for _, hid, _, _ in rows}
    rooms_by_hotel: dict[int, list] = {}
    if hotel_ids:
        for room in (
            await session.scalars(
                select(RoomType)
                .where(RoomType.hotel_id.in_(hotel_ids), RoomType.is_active.is_(True))
                .order_by(RoomType.sort_order)
            )
        ).all():
            rooms_by_hotel.setdefault(room.hotel_id, []).append(room)

    return _render(
        request, user, "unmatched.html", rows=rows, rooms_by_hotel=rooms_by_hotel
    )


# -- notifications ---------------------------------------------------
@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request, user: DashUser, session: DbSession, hours: int = 168
):
    if user is None:
        return _redirect_to_login(request)

    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(Notification, Recipient.name, Hotel.name)
            .join(Recipient, Notification.recipient_id == Recipient.id)
            .outerjoin(Hotel, Notification.hotel_id == Hotel.id)
            .where(Notification.created_at >= since)
            .order_by(Notification.created_at.desc())
            .limit(300)
        )
    ).all()
    recipients = (await session.scalars(select(Recipient).order_by(Recipient.name))).all()

    return _render(
        request, user, "notifications.html",
        notifications=rows, recipients=recipients, hours=hours,
    )


# -- targets ---------------------------------------------------------
@router.get("/targets", response_class=HTMLResponse)
async def targets_page(request: Request, user: DashUser, session: DbSession):
    if user is None:
        return _redirect_to_login(request)

    rows = (
        await session.execute(
            select(MonitorTarget, Hotel.id, Hotel.name, Source.code)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .join(Source, HotelSource.source_id == Source.id)
            .order_by(Hotel.name, MonitorTarget.id)
        )
    ).all()

    # Resolved here rather than in the template, through the same function the
    # dispatcher uses, so the dates shown can never disagree with the dates
    # that will actually be fetched.
    today = local_today()
    resolved_rows = []
    for target, hotel_id, hotel_name, source_code in rows:
        try:
            stay = resolve_stay_window(
                strategy=target.date_strategy,
                today=today,
                fixed_check_in=target.fixed_check_in,
                fixed_check_out=target.fixed_check_out,
                lead_time_days=target.lead_time_days,
                length_of_stay_nights=target.length_of_stay_nights,
            )
        except ValueError:
            stay = None
        resolved_rows.append((target, hotel_id, hotel_name, source_code, stay))

    return _render(request, user, "targets.html", rows=resolved_rows, today=today,
                   now=datetime.now(UTC))


@router.get("/check-runs/{check_run_id}", response_class=HTMLResponse)
async def check_run_partial(
    request: Request, check_run_id: str, user: DashUser, session: DbSession
):
    """The fragment the run button polls.

    A browser fetch takes 20-40 seconds, so the manual-run button gets a 202
    and this endpoint reports progress. Returning a fragment rather than JSON
    keeps the rendering in one place.
    """
    if user is None:
        return _redirect_to_login(request)
    run = await session.get(CheckRun, check_run_id)
    return templates.TemplateResponse(
        request=request, name="partials/check_run.html", context={"run": run}
    )
