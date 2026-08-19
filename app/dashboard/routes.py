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
from zoneinfo import ZoneInfo

import jwt
from fastapi import APIRouter, Cookie, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.adapters.engines import known_engines
from app.api.deps import SESSION_COOKIE, DbSession
from app.config import get_settings
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


def _localtime(value, fmt: str = "%d %b %H:%M") -> str:
    """Render a timestamp in the deployment's timezone, not UTC.

    Everything is stored timezone-aware in UTC, which is correct. Showing it
    that way is not: this system watches hotels in Tamil Nadu, and a Health tab
    reporting a failure at 09:05 when the operator's clock says 14:35 makes
    every incident harder to place. Rendering server-side in the configured
    zone also means the same string appears in the dashboard and in an alert
    email, rather than the two disagreeing.
    """
    if value is None:
        return "—"
    # A plain time (quiet hours) is already expressed in the recipient's own
    # zone and has no date to convert against, so it is rendered as-is.
    if not isinstance(value, datetime):
        return value.strftime(fmt)
    zone = ZoneInfo(get_settings().timezone)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zone).strftime(fmt)


templates.env.filters["localtime"] = _localtime
templates.env.globals["tz"] = get_settings().timezone


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


async def _render(
    request: Request, user: User, session, template: str, **context
) -> HTMLResponse:
    """Render a page, with the "needs attention" count every screen shows.

    Computed centrally rather than per route: a nav badge that some pages
    forget to populate is worse than none, because it silently reads zero.
    Four cheap counts, and they are the only things that ever require a human.
    """
    now = datetime.now(UTC)
    targets = (
        await session.scalars(select(MonitorTarget).where(MonitorTarget.is_enabled.is_(True)))
    ).all()
    attention = {
        "errors": await session.scalar(
            select(func.count(MonitoringError.id)).where(MonitoringError.resolved_at.is_(None))
        ) or 0,
        "unmatched": await session.scalar(
            select(func.count(UnmatchedOffer.id)).where(UnmatchedOffer.resolved_at.is_(None))
        ) or 0,
        "stale": sum(1 for t in targets if t.is_stale(now)),
        "paused": sum(1 for t in targets if t.circuit_state != CircuitState.CLOSED),
    }
    attention["total"] = sum(attention.values())

    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "user": user,
            "is_admin": user.role.value == "admin",
            "attention": attention,
            **context,
        },
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


    last_success = await session.scalar(
        select(func.max(MonitorTarget.last_success_at))
    )
    summary = {
        "hotels_active": await session.scalar(
            select(func.count(Hotel.id)).where(Hotel.is_active.is_(True))
        ),
        "changes_24h": await session.scalar(
            select(func.count(PriceChange.id)).where(PriceChange.changed_at >= day_ago)
        ),
        # Rendered here rather than in the template so it uses the same
        # timezone conversion as every other timestamp on the page.
        "last_check": _localtime(last_success, "%d %b %H:%M") if last_success else None,
    }

    return await _render(
        request, user, session, "overview.html",
        summary=summary,
        changes=recent_changes,
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

    return await _render(
        request, user, session, "matrix.html",
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
    return await _render(
        request, user, session, "hotels.html", hotels=hotels, target_counts=target_counts, q=q or ""
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

    return await _render(
        request, user, session, "hotel_detail.html",
        hotel=hotel, sources=sources, rooms=rooms, targets=targets,
        prices=prices, runs=runs, now=datetime.now(UTC),
        all_sources=all_sources, attached_ids=attached_ids,
        today=local_today(),
        known_engines=known_engines(),
    )


# -- retired screens -------------------------------------------------
# Kept as redirects rather than deleted: an operator with a bookmark should
# land somewhere useful, not on a 404 that looks like a broken deploy.
@router.get("/health-tab")
async def health_tab_redirect():
    return RedirectResponse(url="/attention", status_code=307)


@router.get("/unmatched")
async def unmatched_redirect():
    return RedirectResponse(url="/attention", status_code=307)


@router.get("/targets")
async def targets_redirect():
    """Per-hotel monitoring lives on the hotel page; problems live in Attention."""
    return RedirectResponse(url="/hotels", status_code=307)


# -- sources (no longer in the nav; engine is detected from the URL) --
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

    return await _render(
        request, user, session, "sources.html",
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

    return await _render(
        request, user, session, "changes.html",
        changes=rows, hotels=hotels, hotel_id=hotel_id, hours=hours,
    )


# -- in-app change popups --------------------------------------------
@router.get("/changes/recent")
async def changes_recent(
    user: DashUser,
    session: DbSession,
    since_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=8, ge=1, le=25),
) -> JSONResponse:
    """Confirmed changes newer than ``since_id``, for the on-screen popup.

    WHY A CURSOR AND NOT ``price_changes.notified``
    ===============================================
    ``notified`` belongs to the email/WhatsApp dispatcher, which sets it even
    when no recipient is assigned to the hotel. Reading it here would put the
    popup and the dispatcher in a race over one flag, and whichever ran first
    would silence the other. A cursor held by the browser keeps them
    independent: switching notifications on later changes nothing here, and a
    change can legitimately be shown on two screens at once.

    The first poll of a browser sends no cursor and is answered with the
    current head and nothing else, so opening the dashboard after a busy night
    does not stack up forty toasts nobody asked for.
    """
    if user is None:
        # 401 rather than the usual redirect: this is fetched, not navigated
        # to, and an HTML login form parsed as JSON is a confusing dead end.
        return JSONResponse({"detail": "not authenticated"}, status_code=401)

    head = await session.scalar(select(func.max(PriceChange.id))) or 0
    if since_id is None or since_id >= head:
        return JSONResponse({"cursor": head, "alerts": [], "more": 0})

    rows = (
        await session.execute(
            select(PriceChange, Hotel.name, RoomType.name, PriceSeries.check_in,
                   PriceSeries.check_out)
            .join(Hotel, PriceChange.hotel_id == Hotel.id)
            .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
            .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(PriceChange.id > since_id, PriceChange.id <= head)
            .order_by(PriceChange.id.desc())
            .limit(limit)
        )
    ).all()
    total = await session.scalar(
        select(func.count(PriceChange.id)).where(
            PriceChange.id > since_id, PriceChange.id <= head
        )
    ) or 0

    return JSONResponse(
        {
            "cursor": head,
            # A market-wide reprice can confirm eighty changes in one cycle.
            # Eighty toasts is the same as none; the surplus becomes one line
            # pointing at the Changes page.
            "more": max(0, total - len(rows)),
            # Oldest first: they stack downward, and reading top to bottom
            # should follow the clock.
            "alerts": [_popup_payload(*row) for row in reversed(rows)],
        }
    )


def _popup_payload(change, hotel_name, room_name, check_in, check_out) -> dict:
    """One toast, with every string already formatted.

    Prices are rendered here rather than in JavaScript because ``money``
    already knows about Indian digit grouping and half-rupee rates, and a
    second implementation in the browser is a second place for ₹1,23,456 to
    come out as ₹123,456.
    """
    direction = change.direction.value
    return {
        "id": change.id,
        "hotel_id": change.hotel_id,
        "hotel": hotel_name,
        "room": room_name or "—",
        "stay": f"{check_in} → {check_out}" if check_in and check_out else "",
        "direction": direction,
        "was": money(change.old_price, change.currency),
        # Sold out is a state, not a price of zero — the same distinction the
        # comparison state machine makes.
        "now": (
            "sold out"
            if direction == "became_unavailable"
            else money(change.new_price, change.currency)
        ),
        "delta": (
            None
            if not change.delta
            else ("+" if direction == "increase" else "−")
            + money(abs(change.delta), change.currency)
        ),
        "delta_pct": (
            None if change.delta_pct is None else f"{abs(change.delta_pct):.1f}%"
        ),
        "when": _localtime(change.changed_at, "%d %b %H:%M"),
        # An intraday move and an overnight move need different wording: "was
        # 1,023.75" is ambiguous when the two prices belong to different
        # nights, and a reader who assumes same-night would draw the wrong
        # conclusion about how fast the hotel is repricing.
        "basis": "overnight" if change.previous_offer_key else "intraday",
        "was_label": "last night" if change.previous_offer_key else "was",
    }


# -- attention -------------------------------------------------------
@router.get("/attention", response_class=HTMLResponse)
async def attention_page(request: Request, user: DashUser, session: DbSession):
    """Everything waiting on a person, in one place.

    Health and Unmatched used to be separate screens listing the same kind of
    thing: work only a human can do. Splitting them meant two tabs to check and
    two chances to forget one.

    Ordered by cost of ignoring, not by loudness. "Gone quiet" leads because a
    target that stopped checking without failing looks perfectly healthy
    everywhere else while the prices silently freeze.
    """
    if user is None:
        return _redirect_to_login(request)

    now = datetime.now(UTC)

    target_rows = (
        await session.execute(
            select(MonitorTarget, Hotel.id, Hotel.name)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .where(MonitorTarget.is_enabled.is_(True))
        )
    ).all()
    stale = [(t, hid, name) for t, hid, name in target_rows if t.is_stale(now)]
    paused = [
        (t, hid, name) for t, hid, name in target_rows
        if t.circuit_state != CircuitState.CLOSED
    ]

    unmatched = (
        await session.execute(
            select(UnmatchedOffer, Hotel.id, Hotel.name, RoomType.name)
            .join(HotelSource, UnmatchedOffer.hotel_source_id == HotelSource.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .outerjoin(RoomType, UnmatchedOffer.suggested_room_type_id == RoomType.id)
            .where(UnmatchedOffer.resolved_at.is_(None))
            .order_by(UnmatchedOffer.occurrence_count.desc())
            .limit(100)
        )
    ).all()

    rooms_by_hotel: dict[int, list] = {}
    hotel_ids = {hid for _, hid, _, _ in unmatched}
    if hotel_ids:
        for room in (
            await session.scalars(
                select(RoomType)
                .where(RoomType.hotel_id.in_(hotel_ids), RoomType.is_active.is_(True))
                .order_by(RoomType.sort_order)
            )
        ).all():
            rooms_by_hotel.setdefault(room.hotel_id, []).append(room)

    errors = (
        await session.execute(
            select(MonitoringError, Hotel.name)
            .outerjoin(Hotel, MonitoringError.hotel_id == Hotel.id)
            .where(MonitoringError.resolved_at.is_(None))
            .order_by(MonitoringError.occurred_at.desc())
            .limit(100)
        )
    ).all()

    by_class: dict[str, int] = {}
    for error, _ in errors:
        by_class[error.error_class.value] = by_class.get(error.error_class.value, 0) + 1

    return await _render(
        request, user, session, "attention.html",
        stale=stale, paused=paused, unmatched=unmatched,
        rooms_by_hotel=rooms_by_hotel, errors=errors, by_class=by_class, now=now,
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

    return await _render(
        request, user, session, "notifications.html",
        notifications=rows, recipients=recipients, hours=hours,
    )


# -- targets ---------------------------------------------------------
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
