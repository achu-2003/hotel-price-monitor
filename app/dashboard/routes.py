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

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from types import SimpleNamespace

import jwt
from fastapi import APIRouter, Cookie, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from annotated_types import Ge, Le
from pydantic import BeforeValidator
from sqlalchemy import ARRAY, BigInteger, func, or_, select
from sqlalchemy import cast as sa_cast

from app.adapters.engines import known_engines
from app.api.deps import SESSION_COOKIE, DbSession
from app.config import get_settings
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db.models import (
    AlertDefaults,
    ChangeDirection,
    CheckRun,
    CircuitState,
    Hotel,
    HotelRecipient,
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
from app.db.models.price import SUPPRESSION_LABELS
from app.notifications import registry
from app.schemas.notifications import MAX_ALERT_NUMBERS
from app.notifications.render import money
from app.services import monitoring as monitoring_service
from app.services.dates import local_today, next_weekend
from app.services.ownership import owned_hotel_ids, owns, scope_hotels
from app.services.room_category import CATEGORIES, classify, is_category, label_for

router = APIRouter(include_in_schema=False)


def _blank_as_none(value: object) -> object:
    """Read an empty query parameter as "not given".

    A browser GET form submits every control it has, including the ones left
    empty: "All hotels" is ``<option value="">``, and a cleared ``<input
    type=date>`` sends the empty string. So pressing Filter produces
    ``/changes?hotel_id=&date_from=&date_to=`` — and an ``int | None``
    annotation rejects ``""`` with a 422, which reaches the person as a wall of
    JSON where their unfiltered page should be. They are shown an error for
    using the control the page gave them.

    Fixed here rather than by stripping blanks in JavaScript before submit: the
    query string is also something people type, edit, bookmark and share, and a
    URL that only works when a script rewrote it first is a trap. A plain form
    cannot omit a field, so the server has to accept what forms actually send.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


#: Optional query parameters that a browser form may legitimately submit as "".
#:
#: Where there is a bound, it goes INSIDE the optional and the blank-coercion
#: outside it. That reads backwards and is the only arrangement that works:
#: ``ge`` applied to an ``int | None`` is eventually handed the None and raises
#: ``'>=' not supported between instances of 'NoneType' and 'int'`` — which
#: surfaces as a 500, or as a 422 blaming the caller for a comparison the
#: framework could not make. Nesting keeps the bound on the int alone.
BlankableInt = Annotated[int | None, BeforeValidator(_blank_as_none)]
BlankableDate = Annotated[date | None, BeforeValidator(_blank_as_none)]
BlankableAdults = Annotated[
    Annotated[int, Ge(1), Le(20)] | None, BeforeValidator(_blank_as_none)
]
BlankableRowId = Annotated[
    Annotated[int, Ge(0)] | None, BeforeValidator(_blank_as_none)
]
log = get_logger("dashboard")

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# Available in every template: prices are shown in Indian grouping everywhere,
# and reimplementing that in Jinja would be a second place to get it wrong.
templates.env.globals["money"] = money
templates.env.globals["now"] = lambda: datetime.now(UTC)


def _asset_version(name: str) -> str:
    """A stylesheet's modification time, for the URL that requests it.

    WHY A VERSION AND NOT JUST A PATH
    =================================
    /static/app.css never changed its address, so a browser holding yesterday's
    copy had no way to learn there was a new one short of being told to
    revalidate. A fixed layout was corrected, deployed, and reported still
    broken three times over -- the file on disk right, the file on screen
    stale, and no way to tell those apart by looking at the page.

    The mtime is read on every render rather than cached at import: the whole
    point is to notice a file that changed underneath a running process, and a
    value captured at startup would go stale exactly when it mattered. It is
    one stat() per page against a local file, next to a database round trip.

    Falls back to a constant if the file cannot be read, which loses cache
    busting rather than the page.
    """
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except OSError:  # pragma: no cover - a missing asset is the mount's problem
        return "0"


STATIC_DIR = Path(__file__).parent.parent / "static"
templates.env.globals["asset_version"] = _asset_version


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
    # Scoped like every page it appears on. A badge counting the whole
    # deployment would send someone to an Attention page that then showed
    # them nothing -- a permanent unread marker they have no way to clear.
    mine = owned_hotel_ids(user)
    targets = (
        await session.scalars(
            select(MonitorTarget)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .where(MonitorTarget.is_enabled.is_(True), HotelSource.hotel_id.in_(mine))
        )
    ).all()
    attention = {
        "errors": await session.scalar(
            select(func.count(MonitoringError.id)).where(
                MonitoringError.resolved_at.is_(None),
                or_(
                    MonitoringError.hotel_id.is_(None),
                    MonitoringError.hotel_id.in_(mine),
                ),
            )
        ) or 0,
        "unmatched": await session.scalar(
            select(func.count(UnmatchedOffer.id))
            .join(HotelSource, UnmatchedOffer.hotel_source_id == HotelSource.id)
            .where(UnmatchedOffer.resolved_at.is_(None), HotelSource.hotel_id.in_(mine))
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

    recent_changes = (
        await session.execute(
            select(PriceChange, Hotel.name, RoomType.name)
            .join(Hotel, PriceChange.hotel_id == Hotel.id)
            .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
            .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(PriceChange.changed_at >= day_ago, Hotel.owner_user_id == user.id)
            .order_by(PriceChange.changed_at.desc())
            .limit(20)
        )
    ).all()


    mine = owned_hotel_ids(user)

    last_success = await session.scalar(
        select(func.max(MonitorTarget.last_success_at))
        .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
        .where(HotelSource.hotel_id.in_(mine))
    )
    summary = {
        "hotels_active": await session.scalar(
            select(func.count(Hotel.id)).where(
                Hotel.is_active.is_(True), Hotel.owner_user_id == user.id
            )
        ),
        "changes_24h": await session.scalar(
            select(func.count(PriceChange.id)).where(
                PriceChange.changed_at >= day_ago, PriceChange.hotel_id.in_(mine)
            )
        ),
        # Rendered here rather than in the template so it uses the same
        # timezone conversion as every other timestamp on the page.
        "last_check": _localtime(last_success, "%d %b %H:%M") if last_success else None,
        "last_check_ago": _ago(last_success, now),
    }

    # Split the 24h count by direction. "5 changes" and "5 increases" are
    # different news to someone deciding whether to reprice.
    summary["increases_24h"] = await session.scalar(
        select(func.count(PriceChange.id)).where(
            PriceChange.changed_at >= day_ago,
            PriceChange.direction == ChangeDirection.INCREASE,
            PriceChange.hotel_id.in_(mine),
        )
    )

    hotels = await _tonight_by_hotel(session, now, user)
    summary["rooms_today"] = sum(h["rooms"] for h in hotels)

    return await _render(
        request, user, session, "overview.html",
        summary=summary,
        changes=recent_changes,
        hotels=hotels,
        flat_move=_flat_move(recent_changes),
        max_pct=max((abs(c.delta_pct or 0) for c, _, _ in recent_changes), default=0) or 1,
    )


def _ago(moment: datetime | None, now: datetime) -> str | None:
    """"18 minutes ago" — the form that answers "are these prices current?".

    A clock time alone makes the reader do the subtraction, and the whole
    question this tile exists for is how long ago, not when.
    """
    if moment is None:
        return None
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {rest:02d}m ago"
    return f"{hours // 24}d ago"


def _flat_move(changes) -> dict | None:
    """A move of the IDENTICAL rupee amount across differently-priced rooms.

    Rooms priced from 1,000 to 3,600 do not all rise by exactly 81.25 because
    a hotel repriced them; that is a per-booking fee or levy folded into the
    rate. The table shows five separate rows and hides the one fact that
    explains all of them, so it is stated once, above the noise.
    """
    priced = [c for c, _, _ in changes if c.delta is not None and c.delta != 0]
    if len(priced) < 3:
        return None
    deltas = {c.delta for c in priced}
    if len(deltas) != 1:
        return None
    hotels = {c.hotel_id for c in priced}
    if len(hotels) != 1:
        return None
    return {"delta": priced[0].delta, "currency": priced[0].currency, "rooms": len(priced)}


async def _tonight_by_hotel(session, now: datetime, user) -> list[dict]:
    """One card per hotel: how many rooms, how cheap, how fresh, and whether
    the freshness can be trusted.

    A hotel with prices on screen and no successful check behind them is the
    failure this page exists to make visible, so the status is derived from
    the check rather than from the presence of rows.
    """
    settings = get_settings()
    today = local_today(settings.timezone)

    rows = (
        await session.execute(
            select(
                Hotel.id,
                Hotel.name,
                func.count(PriceSeries.offer_key),
                func.min(PriceSeries.current_price),
                func.max(PriceSeries.last_checked_at),
            )
            .join(
                PriceSeries,
                (PriceSeries.hotel_id == Hotel.id) & (PriceSeries.check_in == today),
                isouter=True,
            )
            .where(Hotel.is_active.is_(True), Hotel.owner_user_id == user.id)
            .group_by(Hotel.id, Hotel.name)
            .order_by(Hotel.name)
        )
    ).all()

    # Standing-rate sources quote one price regardless of date. Their rows are
    # correct but they are not tracking nightly movement, and a card that said
    # "Live" would overstate what is being watched.
    standing = {
        hotel_id
        for hotel_id, config in (
            await session.execute(
                select(HotelSource.hotel_id, HotelSource.adapter_config)
                .where(HotelSource.hotel_id.in_(owned_hotel_ids(user)))
            )
        ).all()
        if (config or {}).get("standing_rate")
    }

    # Staleness is decided by the same rule the silence alarm uses -- no
    # success in three intervals -- rather than a second threshold invented
    # here. Two definitions of "stale" that disagree is how a hotel ends up
    # green on one screen and red on another.
    quiet = {
        hotel_id
        for hotel_id, target in (
            await session.execute(
                select(HotelSource.hotel_id, MonitorTarget).join(
                    MonitorTarget, MonitorTarget.hotel_source_id == HotelSource.id
                ).where(
                    MonitorTarget.is_enabled.is_(True),
                    HotelSource.hotel_id.in_(owned_hotel_ids(user)),
                )
            )
        ).all()
        if target.is_stale(now)
    }

    out = []
    for hotel_id, name, rooms, cheapest, checked in rows:
        if not rooms:
            status, tone = "No prices", "bad"
        elif hotel_id in quiet:
            status, tone = "Gone quiet", "warn"
        elif hotel_id in standing:
            status, tone = "Today only", "warn"
        else:
            status, tone = "Live", "ok"
        out.append({
            "id": hotel_id,
            "name": name,
            "rooms": rooms or 0,
            "cheapest": cheapest,
            "checked": _localtime(checked, "%H:%M") if checked else None,
            "status": status,
            "tone": tone,
            "standing": hotel_id in standing,
        })
    return out


# -- price matrix ----------------------------------------------------
async def _default_night(session, user, adults: int) -> tuple[date, date, str]:
    """The night the comparison screen opens on, and the sentence saying why.

    A NIGHT ONE HOTEL HAS IS NOT A NIGHT WORTH COMPARING
    ====================================================
    This used to be ``MAX(check_in)`` over everything collected, which is the
    right answer only while every hotel is priced for the same nights. It is
    not, and the exception is a feature: when a hotel is sold out for the
    night that was asked for, the fetcher rolls forward and prices the NEXT
    night too, filed under its own dates (see ``_with_rollover`` in
    app/workers/tasks_fetch.py).

    That roll-forward night is then the newest ``check_in`` in the table, and
    it belongs to the one hotel that happened to be full. On 4 Sep the matrix
    opened on 5 Sep and showed a single property -- Sterling, the only hotel
    with no rooms for tonight -- while the ten hotels priced for tonight, the
    comparison the page exists to make, sat one date-picker click away with
    nothing on the page to suggest it.

    So the default is the most recent night that was actually MONITORED: a
    night some check run for one of this account's hotels asked about. A
    rolled night was never asked about by anybody, so it stops being the
    default while staying perfectly reachable by typing the date.

    Deliberately not "the night with the most hotels on it". A hotel whose
    fetch failed this morning would drop today below yesterday's count and
    swing the whole page back a day -- stale prices, presented as current,
    because one property was briefly unreachable.
    """
    owned = owned_hotel_ids(user)
    collected = (
        select(PriceSeries.check_in, PriceSeries.check_out)
        .where(
            PriceSeries.adults == adults,
            PriceSeries.hotel_id.in_(owned),
        )
        .order_by(PriceSeries.check_in.desc())
    )
    # Correlated on the two date columns of the row being considered: "was
    # this night ever asked about, for a hotel of yours?"
    #
    # OUTER, and null targets count. A price typed into the dashboard by hand
    # files a check run with no monitor target at all (the manual-entry
    # endpoint in app/api/v1/targets.py), because nothing scheduled it -- an
    # operator did. Joining through the target dropped every one of those, so
    # a hotel with no booking engine, whose prices only ever arrive by hand,
    # would have had its nights treated exactly like a roll-forward: never the
    # default, however recent. Manual entry is the fallback this system
    # promises those hotels, not a second-class reading.
    #
    # A target-less run cannot be traced back to a hotel, so it cannot be
    # ownership-scoped. It does not need to be: the night it nominates still
    # has to appear in THIS account's price rows above to be chosen at all, so
    # the worst it can do is agree with a night the account already has.
    monitored = (
        select(CheckRun.id)
        .outerjoin(MonitorTarget, MonitorTarget.id == CheckRun.monitor_target_id)
        .outerjoin(HotelSource, HotelSource.id == MonitorTarget.hotel_source_id)
        .where(
            CheckRun.check_in == PriceSeries.check_in,
            CheckRun.check_out == PriceSeries.check_out,
            or_(
                CheckRun.monitor_target_id.is_(None),
                HotelSource.hotel_id.in_(owned),
            ),
        )
        .exists()
    )

    latest = (await session.execute(collected.where(monitored).limit(1))).first()
    if latest is not None:
        return latest.check_in, latest.check_out, "Showing the most recent night monitored."

    # No collected night can be traced to a run at all -- history older than
    # the check-run table, or a restored database that brought the prices and
    # not the runs. The old behaviour is still the best available answer, and
    # showing a rolled night beats showing none.
    latest = (await session.execute(collected.limit(1))).first()
    if latest is not None:
        return latest.check_in, latest.check_out, "Showing the most recent night collected."

    # Nothing has ever been collected, so there is no evidence to prefer. The
    # weekend is as good a first guess as any, and the empty state below
    # explains itself.
    weekend = next_weekend(local_today())
    return weekend.check_in, weekend.check_out, "Defaults to the coming weekend."


def _matrix_groups(rows, recent_cutoff: datetime, category: str | None):
    """Hotels with their room cells, plus how many rooms each category has.

    THE COUNTS ARE OF EVERY ROOM, THE CELLS ARE OF THE CHOSEN ONES
    ==============================================================
    Both come out of one pass over the same rows, and they deliberately
    disagree: a filter chip has to keep saying "Suite 7" while the page is
    filtered to classic rooms, or choosing a category would empty every other
    chip and there would be no way back except the browser's Back button.

    A hotel with no room in the chosen category is absent from the result
    rather than present and empty. Its row would say nothing except that the
    filter is on, which the chips already say, and the comparison reads better
    as the list of properties that actually sell this category.

    ``cheapest`` is the cheapest of the cells that SURVIVED the filter, so
    filtering to "Classic Room" answers "who is cheapest on entry-level
    tonight" rather than repeating the property's overall floor.
    """
    grouped: dict[int, dict] = {}
    counts: dict[str, int] = {}

    for series, hotel, room_name in rows:
        slug = classify(room_name)
        counts[slug] = counts.get(slug, 0) + 1
        if category is not None and slug != category:
            continue

        entry = grouped.setdefault(
            hotel.id,
            {"hotel": hotel, "cells": [], "cheapest": None},
        )
        entry["cells"].append(
            {
                "room_name": room_name,
                "offer_key": series.offer_key,
                "category": slug,
                "category_label": label_for(slug),
                "price": series.current_price,
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

    return grouped, counts


@router.get("/matrix", response_class=HTMLResponse)
async def matrix(
    request: Request,
    user: DashUser,
    session: DbSession,
    check_in: BlankableDate = None,
    check_out: BlankableDate = None,
    adults: BlankableAdults = None,
    category: str | None = None,
):
    """All hotels x rooms for one night. The comparison screen.

    DEFAULTS TO A NIGHT THAT HAS BEEN COLLECTED, which is not the same as the
    night one would most like to see. It defaulted to the coming weekend --
    the window where rates move most, and a good answer if anything were
    watching it. Every enabled target on this deployment is rolling with a
    lead time of zero, so the only nights that ever have prices are tonight
    and the recent past, and the comparison screen opened empty every single
    day: "Nothing has been collected for these dates" about dates the page
    itself had chosen.

    A default is a guess at what someone wants to see. Guessing at data that
    exists beats guessing at data that would be more interesting. Which night
    exactly, and why not simply the newest one, is ``_default_night`` above.
    """
    if user is None:
        return _redirect_to_login(request)

    # Clearing the number input submits ``adults=``, which is a request for the
    # default rather than a request for nothing -- the query below compares it
    # to a NOT NULL column and would match no rows at all.
    if adults is None:
        adults = 2

    # A category the code does not have -- an empty string from the "All" chip,
    # a slug from an older bookmark, a typo in a shared URL -- shows everything
    # rather than 422s or shows nothing. The chips below then make the real
    # choices visible, which a validation error would not.
    if not is_category(category):
        category = None

    default_note = None
    if check_in is None or check_out is None:
        check_in, check_out, default_note = await _default_night(session, user, adults)

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
                Hotel.owner_user_id == user.id,
            )
            .order_by(Hotel.name, RoomType.sort_order)
        )
    ).all()

    recent_cutoff = datetime.now(UTC) - timedelta(hours=24)
    grouped, counts = _matrix_groups(rows, recent_cutoff, category)

    # One chip per category that has a room on this night, in the order of the
    # sheet this page replaced. A category nothing is sold under is left out
    # rather than shown at zero: a dead chip is a control that does nothing,
    # and a row of them would bury the ones that work.
    # The chosen one is kept even at zero, so a link somebody shared for a
    # category nothing is sold under tonight still shows WHICH filter is on.
    # Without it the page reads as broken: an empty grid and no chip lit.
    chips = [
        {"slug": c.slug, "label": c.label, "count": counts.get(c.slug, 0)}
        for c in CATEGORIES
        if counts.get(c.slug) or c.slug == category
    ]

    # Rooms exist for this night, but none in the chosen category. That is a
    # different sentence from "nothing has been collected", and saying the
    # wrong one sends someone off to debug a fetcher that is working.
    filtered_out = bool(rows) and not grouped

    # Which nights DO have prices, for the empty state. "Nothing here" is a
    # dead end; "nothing here, and these are the nights that have something"
    # is the answer to the question the empty page provokes -- and on a
    # deployment whose targets all run at zero lead time, it is also the
    # explanation for why every other date is blank.
    #
    # Not asked when the filter is what emptied the page: this night HAS
    # prices, so a list of other nights to try would be answering a question
    # nobody asked, and it costs a query to be wrong.
    collected: list = []
    if not grouped and not filtered_out:
        collected = (
            await session.execute(
                select(PriceSeries.check_in, PriceSeries.check_out, PriceSeries.adults)
                .where(PriceSeries.hotel_id.in_(owned_hotel_ids(user)))
                .group_by(PriceSeries.check_in, PriceSeries.check_out, PriceSeries.adults)
                .order_by(PriceSeries.check_in.desc())
                .limit(6)
            )
        ).all()

    return await _render(
        request, user, session, "matrix.html",
        rows=sorted(grouped.values(), key=lambda e: e["hotel"].name),
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        category=category,
        category_label=label_for(category) if category else None,
        chips=chips,
        filtered_out=filtered_out,
        default_note=default_note,
        collected=collected,
    )


# -- hotels ----------------------------------------------------------
@router.get("/hotels", response_class=HTMLResponse)
async def hotels_page(
    request: Request, user: DashUser, session: DbSession, q: str | None = None
):
    if user is None:
        return _redirect_to_login(request)

    statement = scope_hotels(select(Hotel), user).order_by(Hotel.name)
    if q:
        statement = statement.where(Hotel.name.ilike(f"%{q}%"))
    hotels = (await session.scalars(statement)).all()

    target_counts = dict(
        (
            await session.execute(
                select(HotelSource.hotel_id, func.count(MonitorTarget.id))
                .join(MonitorTarget, MonitorTarget.hotel_source_id == HotelSource.id)
                .where(HotelSource.hotel_id.in_(owned_hotel_ids(user)))
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
    if not owns(hotel, user):
        # Same redirect for "no such hotel" and "not yours". A distinct
        # "forbidden" page would confirm the id belongs to a real property
        # somebody is watching.
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
    # TONIGHT ONLY -- the same rule the hotels list and the matrix already use.
    #
    # Every target is a rolling one-night window, so a fresh series is created
    # each day and yesterday's is frozen at whatever the last check before
    # check-in found. Unfiltered and sorted oldest-first, this table therefore
    # led with the oldest night on record: days stale, and disagreeing with the
    # hotel's own booking page. That is the failure this product exists to
    # catch, showing up on its own dashboard.
    #
    # Restricting the screen to today is what makes every number on it
    # checkable against the hotel's site right now. Past nights are not
    # deleted -- the history is in price_observations, and the Changes screen
    # still reports last night's closing price against tonight's opening one.
    today = local_today(get_settings().timezone)
    prices = (
        await session.execute(
            select(PriceSeries, RoomType.name)
            .join(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(PriceSeries.hotel_id == hotel_id, PriceSeries.check_in == today)
            .order_by(RoomType.sort_order, PriceSeries.adults)
            .limit(100)
        )
    ).all()

    # Whether anything was ever collected for another night, so a hotel that has
    # stopped collecting says so instead of falling back to the first-run setup
    # path and looking like it was never configured.
    had_past_prices = bool(
        await session.scalar(
            select(func.count())
            .select_from(PriceSeries)
            .where(PriceSeries.hotel_id == hotel_id, PriceSeries.check_in != today)
        )
    )
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

    # The deployment default, so the per-hotel panel can show what this hotel
    # would inherit if its override were switched off.
    stored_defaults = await session.get(AlertDefaults, 1)
    if stored_defaults is None:
        current = monitoring_service.default_thresholds()
        stored_defaults = SimpleNamespace(
            min_delta_abs=current.min_delta_abs,
            min_delta_pct=current.min_delta_pct,
            confirm_checks=current.confirm_checks,
        )
    alert_defaults = stored_defaults

    return await _render(
        request, user, session, "hotel_detail.html",
        alert_defaults=alert_defaults,
        hotel=hotel, sources=sources, rooms=rooms, targets=targets,
        prices=prices, runs=runs, now=datetime.now(UTC),
        all_sources=all_sources, attached_ids=attached_ids,
        today=today, had_past_prices=had_past_prices,
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
    hotel_id: BlankableInt = None,
    hours: int = Query(default=48, ge=1, le=720),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """Confirmed changes, filtered by hotel and by time.

    Two ways to say "when", because they answer different questions. The
    rolling window is for "what has moved lately" and is what the page opens
    with; explicit dates are for "what happened on the day that guest
    complained", which a 48-hour preset cannot express at all once the day is
    three weeks back.

    Explicit dates WIN when given: a page showing both controls and quietly
    obeying the other one is worse than either.
    """
    if user is None:
        return _redirect_to_login(request)

    tz = ZoneInfo(get_settings().timezone)
    from_date = _parse_date(date_from)
    to_date = _parse_date(date_to)

    statement = scope_hotels(
        select(PriceChange, Hotel.name, RoomType.name, PriceSeries.check_in,
               PriceSeries.check_out)
        .join(Hotel, PriceChange.hotel_id == Hotel.id)
        .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
        .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id),
        user,
    )

    if from_date or to_date:
        # Dates are read in the hotel's timezone, not the server's. A person
        # asking for "19 Aug" means their 19 Aug, and the stored timestamps are
        # UTC -- so a naive comparison quietly loses the last five and a half
        # hours of the day here.
        if from_date:
            start = datetime.combine(from_date, time.min, tzinfo=tz)
            statement = statement.where(PriceChange.changed_at >= start)
        if to_date:
            end = datetime.combine(to_date, time.min, tzinfo=tz) + timedelta(days=1)
            statement = statement.where(PriceChange.changed_at < end)
    else:
        statement = statement.where(
            PriceChange.changed_at >= datetime.now(UTC) - timedelta(hours=hours)
        )

    if hotel_id is not None:
        statement = statement.where(PriceChange.hotel_id == hotel_id)

    rows = (
        await session.execute(
            statement.order_by(PriceChange.changed_at.desc()).limit(300)
        )
    ).all()
    hotels = (
        await session.scalars(
            select(Hotel)
            .where(Hotel.is_active.is_(True), Hotel.owner_user_id == user.id)
            .order_by(Hotel.name)
        )
    ).all()

    return await _render(
        request, user, session, "changes.html",
        changes=rows, hotels=hotels, hotel_id=hotel_id, hours=hours,
        date_from=from_date.isoformat() if from_date else "",
        date_to=to_date.isoformat() if to_date else "",
        delivery=await _delivery_state(session, [row[0] for row in rows]),
    )


def _parse_date(raw: str | None) -> date | None:
    """An unparseable date is ignored rather than raising a 422 at someone."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


async def _delivery_state(session, changes) -> dict[int, tuple[str, str]]:
    """Did anyone actually hear about each change? (label, css class).

    ``price_changes.notified`` cannot answer this. The dispatcher sets it even
    when NO recipient is assigned to the hotel -- deliberately, so the change
    does not reappear in every sweep forever -- which meant a column headed
    "Told" showed "yes" for every row on an installation with no recipients at
    all. It reported that the work was finished, and read as though somebody
    had been informed.

    So the notifications actually written are consulted instead, and the honest
    answer is one of four: sent, still queued, failed, or nobody to tell.
    """
    if not changes:
        return {}

    ids = [c.id for c in changes]
    # `&&` is PostgreSQL's array-overlap operator, reached through op() because
    # the column is declared with the generic ARRAY type, whose comparator has
    # no overlap(). One statement rather than one per change.
    rows = (
        await session.execute(
            select(Notification.status, Notification.price_change_ids).where(
                Notification.price_change_ids.op("&&")(
                    sa_cast(ids, ARRAY(BigInteger))
                )
            )
        )
    ).all()

    best: dict[int, str] = {}
    rank = {"sent": 3, "delivered": 4, "queued": 2, "held": 2, "failed": 1}
    for status, change_ids in rows:
        name = getattr(status, "value", str(status))
        for change_id in change_ids or []:
            if change_id not in ids:
                continue
            if rank.get(name, 0) >= rank.get(best.get(change_id, ""), 0):
                best[change_id] = name

    state: dict[int, tuple[str, str]] = {}
    for change in changes:
        name = best.get(change.id)
        if name in ("sent", "delivered"):
            state[change.id] = ("sent", "pill-ok")
        elif name == "failed":
            state[change.id] = ("failed", "pill-stop")
        elif name is not None:
            state[change.id] = ("queued", "pill-warn")
        elif change.notified:
            # Processed, with nobody assigned to hear it. The dispatcher now
            # records WHICH of the three reasons applied, because they need
            # three different fixes -- assign somebody, reactivate them, or
            # lower a threshold. Older rows predate the column and fall back to
            # the vague answer rather than to a guessed specific one.
            reason = getattr(change, "suppressed_reason", None)
            state[change.id] = (
                SUPPRESSION_LABELS.get(reason, "no one to tell"),
                "pill-off",
            )
        else:
            state[change.id] = ("pending", "pill-pending")
    return state


# -- in-app change popups --------------------------------------------
@router.get("/changes/recent")
async def changes_recent(
    user: DashUser,
    session: DbSession,
    since_id: BlankableRowId = None,
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

    mine = owned_hotel_ids(user)
    head = await session.scalar(
        select(func.max(PriceChange.id)).where(PriceChange.hotel_id.in_(mine))
    ) or 0
    if since_id is None or since_id >= head:
        return JSONResponse({"cursor": head, "alerts": [], "more": 0})

    rows = (
        await session.execute(
            select(PriceChange, Hotel.name, RoomType.name, PriceSeries.check_in,
                   PriceSeries.check_out)
            .join(Hotel, PriceChange.hotel_id == Hotel.id)
            .outerjoin(PriceSeries, PriceChange.offer_key == PriceSeries.offer_key)
            .outerjoin(RoomType, PriceSeries.room_type_id == RoomType.id)
            .where(
                PriceChange.id > since_id,
                PriceChange.id <= head,
                PriceChange.hotel_id.in_(mine),
            )
            .order_by(PriceChange.id.desc())
            .limit(limit)
        )
    ).all()
    total = await session.scalar(
        select(func.count(PriceChange.id)).where(
            PriceChange.id > since_id,
            PriceChange.id <= head,
            PriceChange.hotel_id.in_(mine),
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
            .where(MonitorTarget.is_enabled.is_(True), Hotel.owner_user_id == user.id)
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
            .where(UnmatchedOffer.resolved_at.is_(None), Hotel.owner_user_id == user.id)
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
            .where(
                MonitoringError.resolved_at.is_(None),
                # Unattributed errors stay: they name no property, and they
                # are how a source that breaks before it resolves to a hotel
                # gets noticed at all.
                or_(
                    MonitoringError.hotel_id.is_(None),
                    MonitoringError.hotel_id.in_(owned_hotel_ids(user)),
                ),
            )
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
    """What was actually sent.

    Who receives alerts moved to /settings. The two were one page while there
    were only a handful of recipients, and the delivery history -- the thing
    somebody opens when a hotel says it never heard -- sat below three hundred
    lines of setup forms they were not looking for.
    """
    if user is None:
        return _redirect_to_login(request)

    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(Notification, Recipient.name, Hotel.name)
            .join(Recipient, Notification.recipient_id == Recipient.id)
            .outerjoin(Hotel, Notification.hotel_id == Hotel.id)
            .where(
                Notification.created_at >= since,
                Notification.hotel_id.in_(owned_hotel_ids(user)),
            )
            .order_by(Notification.created_at.desc())
            .limit(300)
        )
    ).all()

    return await _render(
        request, user, session, "notifications.html",
        notifications=rows, hours=hours,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: DashUser, session: DbSession):
    """Who the system may tell, and how to reach them."""
    if user is None:
        return _redirect_to_login(request)

    # Alert sensitivity, and the two prices that make it concrete. Both floors
    # have to be cleared, so the same rupee amount is loud on a cheap room and
    # silent on an expensive one -- the panel shows what it means here rather
    # than explaining the rule in the abstract.
    stored = await session.get(AlertDefaults, 1)
    if stored is None:
        current = monitoring_service.default_thresholds()
        stored = AlertDefaults(
            id=1,
            min_delta_abs=current.min_delta_abs,
            min_delta_pct=current.min_delta_pct,
            confirm_checks=current.confirm_checks,
        )
    cheapest, dearest = (
        await session.execute(
            select(func.min(PriceSeries.last_price), func.max(PriceSeries.last_price))
            .join(Hotel, Hotel.id == PriceSeries.hotel_id)
            .where(PriceSeries.last_price.is_not(None), Hotel.owner_user_id == user.id)
        )
    ).one()
    alert_defaults = SimpleNamespace(
        min_delta_abs=stored.min_delta_abs,
        min_delta_pct=stored.min_delta_pct,
        confirm_checks=stored.confirm_checks,
        cheapest_room=cheapest,
        dearest_room=dearest,
    )

    recipients = (await session.scalars(select(Recipient).order_by(Recipient.name))).all()

    # The WhatsApp alert numbers, which are recipients wearing the
    # alerts_all_hotels flag rather than a separate kind of thing -- so they
    # reuse the digest, the dedupe and the delivery history on /notifications.
    alert_numbers = [r for r in recipients if r.alerts_all_hotels and r.is_active]

    # Who each person actually covers. A recipient with no assignment receives
    # nothing at all -- the dispatcher looks up hotel_recipients, not
    # recipients -- so the page has to show the assignment, not just the row.
    assignments: dict[int, list] = {}
    for link, hotel_name in (
        await session.execute(
            select(HotelRecipient, Hotel.name)
            .join(Hotel, HotelRecipient.hotel_id == Hotel.id)
            .where(Hotel.owner_user_id == user.id)
            .order_by(Hotel.name)
        )
    ).all():
        assignments.setdefault(link.recipient_id, []).append((link, hotel_name))

    hotels = (
        await session.scalars(
            select(Hotel)
            .where(Hotel.is_active.is_(True), Hotel.owner_user_id == user.id)
            .order_by(Hotel.name)
        )
    ).all()

    # Only channels this deployment can actually send on. Offering WhatsApp
    # before the access token exists produces an assignment that looks saved
    # and fails at the first price move, which is the worst moment to find out.
    settings = get_settings()
    return await _render(
        request, user, session, "settings.html",
        alert_defaults=alert_defaults,
        recipients=recipients, assignments=assignments, hotels=hotels,
        channels=registry.available_channels(),
        default_quiet=(settings.quiet_hours_start, settings.quiet_hours_end),
        alert_numbers=alert_numbers,
        max_alert_numbers=MAX_ALERT_NUMBERS,
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

    # Scoped through the target's hotel. A run id is a uuid rather than a
    # small integer, but "hard to guess" is not an access rule, and the
    # fragment reports what a competitor's check found. A run with no target
    # came from manual entry and carries no hotel to check it against.
    run = await session.scalar(
        select(CheckRun).where(
            CheckRun.id == check_run_id,
            or_(
                CheckRun.monitor_target_id.is_(None),
                CheckRun.monitor_target_id.in_(
                    select(MonitorTarget.id)
                    .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
                    .where(HotelSource.hotel_id.in_(owned_hotel_ids(user)))
                ),
            ),
        )
    )
    return templates.TemplateResponse(
        request=request, name="partials/check_run.html", context={"run": run}
    )
