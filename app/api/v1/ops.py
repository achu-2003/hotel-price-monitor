"""Health, readiness, errors, and the dashboard summary.

The distinction between ``/health`` and ``/health/ready`` is not pedantry.
Liveness must answer "is this process alive" and nothing else: if it also
checked the database, a thirty-second Postgres blip would make an orchestrator
kill and restart every API container, turning a brief degradation into an
outage and a restart loop.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, text

from app.api.deps import AdminUser, CurrentUser, DbSession, get_object_or_404, record_audit
from app.core.errors import ErrorClass
from app.core.logging import get_logger
from app.db.models import (
    CheckRun,
    CircuitState,
    Hotel,
    HotelSource,
    Notification,
    NotificationStatus,
    MonitoringError,
    MonitorTarget,
    PriceChange,
    UnmatchedOffer,
)
from app.schemas.common import HealthStatus, Page, ReadinessStatus
from app.schemas.monitoring import MonitoringErrorOut
from app.schemas.prices import DashboardSummary
from app.services.rediscovery import REPAIRABLE, RepairState

router = APIRouter(tags=["ops"])
log = get_logger("api.ops")


@router.get("/health", response_model=HealthStatus, include_in_schema=False)
async def health():
    """Liveness. Deliberately checks nothing external."""
    return HealthStatus(status="ok", checked_at=datetime.now(UTC))


@router.get("/health/ready", response_model=ReadinessStatus, include_in_schema=False)
async def readiness(session: DbSession, response: Response):
    """Readiness. Checks the things this process cannot work without.

    Returns 503 when a dependency is down so a load balancer stops sending
    traffic — without the container being killed, which is what liveness is
    for.
    """
    database_ok = True
    redis_ok = True
    detail = None

    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        database_ok = False
        detail = f"database: {type(exc).__name__}"
        log.warning("readiness_database_failed", error=str(exc))

    try:
        from app.core.ratelimit import get_redis

        get_redis().ping()
    except Exception as exc:  # noqa: BLE001
        redis_ok = False
        detail = f"{detail + '; ' if detail else ''}redis: {type(exc).__name__}"
        log.warning("readiness_redis_failed", error=str(exc))

    ready = database_ok and redis_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessStatus(
        status="ready" if ready else "not_ready",
        database=database_ok,
        redis=redis_ok,
        detail=detail,
    )


@router.get("/errors", response_model=Page[MonitoringErrorOut])
async def list_errors(
    session: DbSession,
    _user: CurrentUser,
    hotel_id: int | None = None,
    error_class: ErrorClass | None = Query(default=None, alias="class"),
    unresolved: bool = True,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """The Health tab's error list, grouped by hotel and class in the UI."""
    statement = select(MonitoringError, Hotel.name).outerjoin(
        Hotel, MonitoringError.hotel_id == Hotel.id
    )
    if hotel_id is not None:
        statement = statement.where(MonitoringError.hotel_id == hotel_id)
    if error_class is not None:
        statement = statement.where(MonitoringError.error_class == error_class)
    if unresolved:
        statement = statement.where(MonitoringError.resolved_at.is_(None))

    rows = (
        await session.execute(
            statement.order_by(MonitoringError.occurred_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    items = []
    for error, hotel_name in rows:
        out = MonitoringErrorOut.model_validate(error)
        out.hotel_name = hotel_name
        out.has_screenshot = bool(error.screenshot_path)
        out.has_html = bool(error.html_path)
        items.append(out)
    return Page[MonitoringErrorOut](items=items)


@router.post("/errors/{error_id}/resolve", response_model=MonitoringErrorOut)
async def resolve_error(
    error_id: int, request: Request, session: DbSession, admin: AdminUser
):
    """Mark an error handled, so the Health tab shows what still needs work.

    Resolving a SELECTOR fault also hands its source's repair budget back.

    Automatic re-discovery gets three attempts per source, and running out is
    meant to say "this one needs a person". Resolve is the person: it is the
    only action the Health tab offers, and it used to close the row and nothing
    else -- so a source that had spent its budget stayed locked out of repair
    permanently. The next check collapsed the same offers, raised the same
    alert, and declined the same repair, and no amount of fixing the scanner
    could ever reach it.

    Restricted to the classes a repair could actually fix, because resolving a
    blocked source or an expired certificate says nothing about whether the
    selectors deserve another try.
    """
    error = await get_object_or_404(session, MonitoringError, error_id, "Error")
    error.resolved_at = datetime.now(UTC)

    restored = await _restore_repair_budget(session, error)

    await record_audit(
        session, user=admin, action="resolve", entity="monitoring_error",
        entity_id=error_id, request=request,
    )
    await session.commit()
    if restored:
        log.info("repair_budget_restored", error_id=error_id, hotel_id=error.hotel_id)
    return MonitoringErrorOut.model_validate(error)


async def _restore_repair_budget(session, error: MonitoringError) -> bool:
    """Give this error's source its re-discovery attempts back. See above.

    Silent about a source it cannot find: an error row keeps its hotel and
    source ids after the pairing itself is deleted, and an alert must stay
    resolvable regardless.
    """
    if str(error.error_class.value) not in REPAIRABLE:
        return False
    if error.hotel_id is None or error.source_id is None:
        return False

    pairing = await session.scalar(
        select(HotelSource).where(
            HotelSource.hotel_id == error.hotel_id,
            HotelSource.source_id == error.source_id,
        )
    )
    if pairing is None:
        return False

    config = dict(pairing.adapter_config or {})
    state = RepairState.from_config(config)
    if not state.attempts and state.last_attempt_at is None:
        return False  # nothing spent, nothing to give back

    pairing.adapter_config = {**config, **state.release()}
    return True


@router.get("/errors/{error_id}/artifact")
async def get_error_artifact(
    error_id: int,
    session: DbSession,
    _user: CurrentUser,
    kind: str = Query(default="screenshot", pattern="^(screenshot|html)$"),
):
    """Serve the saved screenshot or HTML for a failed fetch.

    The stored path is resolved and confirmed to sit inside the artifact
    directory before anything is read. The value comes from our own code
    today, but a path from the database that is used to open a file is exactly
    the shape of a traversal bug, and the check costs nothing.
    """
    from fastapi.responses import FileResponse

    from app.config import get_settings

    error = await get_object_or_404(session, MonitoringError, error_id, "Error")
    stored = error.screenshot_path if kind == "screenshot" else error.html_path
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {kind} was captured for this error.",
        )

    root = Path(get_settings().artifact_dir).resolve()
    path = Path(stored).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        log.warning("artifact_path_rejected", error_id=error_id, path=str(path))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That artifact is no longer available (it may have been pruned).",
        )

    return FileResponse(
        path,
        media_type="image/png" if kind == "screenshot" else "text/html",
        filename=path.name,
    )


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(session: DbSession, _user: CurrentUser):
    """One query per counter, for the dashboard's header row."""
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)

    hotels_active = await session.scalar(
        select(func.count(Hotel.id)).where(Hotel.is_active.is_(True))
    )
    targets = (
        await session.scalars(select(MonitorTarget).where(MonitorTarget.is_enabled.is_(True)))
    ).all()
    checks_last_hour = await session.scalar(
        select(func.count(CheckRun.id)).where(CheckRun.started_at >= hour_ago)
    )
    changes_last_24h = await session.scalar(
        select(func.count(PriceChange.id)).where(PriceChange.changed_at >= day_ago)
    )
    unmatched = await session.scalar(
        select(func.count(UnmatchedOffer.id)).where(UnmatchedOffer.resolved_at.is_(None))
    )
    errors = await session.scalar(
        select(func.count(MonitoringError.id)).where(MonitoringError.resolved_at.is_(None))
    )
    failed_notifications = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.status == NotificationStatus.FAILED,
            Notification.created_at >= day_ago,
        )
    )

    successes = [t.last_success_at for t in targets if t.last_success_at]
    return DashboardSummary(
        hotels_active=hotels_active or 0,
        targets_enabled=len(targets),
        circuits_open=sum(1 for t in targets if t.circuit_state == CircuitState.OPEN),
        checks_last_hour=checks_last_hour or 0,
        changes_last_24h=changes_last_24h or 0,
        unmatched_rooms=unmatched or 0,
        unresolved_errors=errors or 0,
        # The number that matters most on this screen: targets whose prices
        # look current and are actually frozen.
        stale_targets=sum(1 for t in targets if t.is_stale(now)),
        notifications_failed_24h=failed_notifications or 0,
        oldest_successful_check=min(successes) if successes else None,
    )


@router.get("/dashboard/failures", response_model=Page[MonitoringErrorOut])
async def recent_failures(session: DbSession, _user: CurrentUser, hours: int = 24):
    """Everything that broke recently, newest first."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(MonitoringError, Hotel.name)
            .outerjoin(Hotel, MonitoringError.hotel_id == Hotel.id)
            .where(MonitoringError.occurred_at >= since)
            .order_by(MonitoringError.occurred_at.desc())
            .limit(200)
        )
    ).all()

    items = []
    for error, hotel_name in rows:
        out = MonitoringErrorOut.model_validate(error)
        out.hotel_name = hotel_name
        out.has_screenshot = bool(error.screenshot_path)
        out.has_html = bool(error.html_path)
        items.append(out)
    return Page[MonitoringErrorOut](items=items)
