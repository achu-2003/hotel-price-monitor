"""Dispatch, health, and the circuit breaker.

The scheduler's job is to answer one question every 60 seconds: *what should be
fetched right now, and by whom?* Everything about how that question is answered
lives here rather than in the Celery task, so it can be tested against a
database without a broker or a browser.

WHY GROUPS
==========
Several monitor targets frequently resolve to the same page load: two targets
on one hotel watching the same weekend at the same occupancy differ only in
their alert thresholds. One page load already lists every room, so fetching per
target would multiply the load we put on a hotel's site for no extra
information. Targets are therefore grouped by everything that changes the
request — hotel source, dates, occupancy — and each group becomes one task.

CIRCUIT BREAKER
===============
Five consecutive failures open the circuit and the target is paused for an
hour. After that it goes half-open: exactly one probe is allowed, and the next
result decides whether it closes or opens again. The point is to stop hammering
a site that is broken, blocked, or gone, without needing a human to remember to
re-enable it once it recovers.
"""
from __future__ import annotations

import time

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import registry
from app.config import Settings, get_settings
from app.core.errors import ErrorClass, FetchError
from app.core.logging import get_logger
from app.core.redaction import scrub
from app.db.models import (
    CircuitState,
    Hotel,
    HotelSource,
    MonitoringError,
    MonitorTarget,
    Source,
)
from app.services.comparison import Thresholds
from app.services.dates import StayWindow, local_today, resolve_stay_window

log = get_logger("monitoring")

#: Consecutive failures before a target is paused.
FAILURE_THRESHOLD = 5
#: How long an open circuit stays open before a single probe is allowed.
CIRCUIT_COOLDOWN = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DueGroup:
    """One page load: everything needed to fetch it, resolved up front.

    Fully self-describing on purpose. The fetch task receives primitives over
    the broker, not ORM objects, so a worker deploy that lags the scheduler
    cannot deserialise a stale model.
    """

    hotel_id: int
    hotel_name: str
    hotel_source_id: int
    source_id: int
    adapter_key: str
    queue: str
    url: str | None
    external_id: str | None
    currency: str
    adapter_config: dict[str, Any]
    stay: StayWindow
    adults: int
    children: int
    rooms: int
    meal_plan_filter: str | None
    target_ids: list[int]
    rate_limit_per_min: int

    @property
    def lock_key(self) -> str:
        """One in-flight fetch per hotel source and stay window.

        Keyed on the request rather than the target, because two targets that
        resolve to the same page must not both drive a browser at it.
        """
        return (
            f"fetch:{self.hotel_source_id}:{self.stay.check_in.isoformat()}"
            f":{self.stay.check_out.isoformat()}:{self.adults}:{self.children}"
        )


@dataclass(frozen=True, slots=True)
class _GroupKey:
    hotel_source_id: int
    check_in: Any
    check_out: Any
    adults: int
    children: int
    rooms: int
    meal_plan_filter: str | None


def find_due_groups(session: Session, now: datetime | None = None) -> list[DueGroup]:
    """Targets that are due, resolved into concrete page loads.

    Also performs the half-open transition, because "is this target due?" and
    "has this target's circuit cooled down?" are the same decision and
    splitting them across two passes invites them to disagree.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    today = local_today(settings.timezone)

    rows = session.execute(
        select(MonitorTarget, HotelSource, Source, Hotel)
        .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
        .join(Source, HotelSource.source_id == Source.id)
        .join(Hotel, HotelSource.hotel_id == Hotel.id)
        .where(
            MonitorTarget.is_enabled.is_(True),
            HotelSource.is_active.is_(True),
            Hotel.is_active.is_(True),
            Source.is_enabled.is_(True),
            # A source that no human has signed off on is never fetched. This
            # is the compliance gate from the vetting checklist, enforced in
            # the query rather than remembered by an operator.
            Source.tos_reviewed_at.is_not(None),
            MonitorTarget.next_run_at.is_(None) | (MonitorTarget.next_run_at <= now),
        )
        .with_for_update(of=MonitorTarget, skip_locked=True)
    ).all()

    groups: dict[_GroupKey, DueGroup] = {}

    for target, hotel_source, source, hotel in rows:
        if not _circuit_allows(target, now):
            continue

        try:
            queue = registry.queue_for(source.adapter_key)
        except Exception as exc:  # noqa: BLE001 - an unknown key must not stop the sweep
            log.warning(
                "unknown_adapter_key",
                target_id=target.id,
                adapter_key=source.adapter_key,
                error=str(exc),
            )
            continue

        if queue == "manual":
            # Prices for this source arrive from the dashboard. Nothing to
            # schedule, and no reason to log about it every minute.
            continue

        stay = resolve_stay_window(
            strategy=target.date_strategy,
            today=today,
            fixed_check_in=target.fixed_check_in,
            fixed_check_out=target.fixed_check_out,
            lead_time_days=target.lead_time_days,
            length_of_stay_nights=target.length_of_stay_nights,
        )
        if stay is None:
            # A fixed window whose dates have passed. Disable it rather than
            # re-evaluating it every minute for the rest of time.
            target.is_enabled = False
            target.next_run_at = None
            log.info("target_window_expired", target_id=target.id)
            continue

        key = _GroupKey(
            hotel_source_id=hotel_source.id,
            check_in=stay.check_in,
            check_out=stay.check_out,
            adults=target.adults,
            children=target.children,
            rooms=target.rooms,
            meal_plan_filter=target.meal_plan_filter,
        )

        if (existing := groups.get(key)) is not None:
            existing.target_ids.append(target.id)
            continue

        groups[key] = DueGroup(
            hotel_id=hotel.id,
            hotel_name=hotel.name,
            hotel_source_id=hotel_source.id,
            source_id=source.id,
            adapter_key=source.adapter_key,
            queue=queue,
            url=hotel_source.url,
            external_id=hotel_source.external_id,
            currency=hotel_source.currency,
            adapter_config=dict(hotel_source.adapter_config or {}),
            stay=stay,
            adults=target.adults,
            children=target.children,
            rooms=target.rooms,
            meal_plan_filter=target.meal_plan_filter,
            target_ids=[target.id],
            rate_limit_per_min=source.rate_limit_per_min,
        )

    return list(groups.values())


def _circuit_allows(target: MonitorTarget, now: datetime) -> bool:
    """Whether this target may run, transitioning it to half-open if it is time."""
    if target.circuit_state == CircuitState.CLOSED:
        return True

    if target.circuit_state == CircuitState.HALF_OPEN:
        # A probe is already out. Waiting for its verdict is the entire point
        # of half-open; sending a second would tell us nothing new.
        return False

    opened = target.circuit_opened_at
    if opened is not None and now - opened < CIRCUIT_COOLDOWN:
        return False

    target.circuit_state = CircuitState.HALF_OPEN
    log.info("circuit_half_open", target_id=target.id)
    return True


def schedule_next_run(
    session: Session, target_ids: list[int], now: datetime | None = None
) -> None:
    """Push these targets forward by their interval.

    Called at DISPATCH time, not at completion. If it waited for the fetch to
    finish, a task that hung would leave its target permanently due and the
    dispatcher would enqueue it again every 60 seconds — the lock would absorb
    the damage, but the queue would fill with skipped work.
    """
    now = now or datetime.now(UTC)
    targets = session.execute(
        select(MonitorTarget).where(MonitorTarget.id.in_(target_ids))
    ).scalars().all()
    for target in targets:
        target.next_run_at = now + timedelta(minutes=target.interval_minutes)


def record_success(
    session: Session, target_ids: list[int], now: datetime | None = None
) -> None:
    """Clear the failure state and close any half-open circuit."""
    now = now or datetime.now(UTC)
    targets = session.execute(
        select(MonitorTarget).where(MonitorTarget.id.in_(target_ids))
    ).scalars().all()
    for target in targets:
        target.last_success_at = now
        target.consecutive_failures = 0
        if target.circuit_state != CircuitState.CLOSED:
            log.info("circuit_closed", target_id=target.id)
        target.circuit_state = CircuitState.CLOSED
        target.circuit_opened_at = None


def record_failure(
    session: Session,
    target_ids: list[int],
    error: FetchError,
    now: datetime | None = None,
) -> None:
    """Count the failure and open the circuit when it is time.

    Two error classes skip the counter and open immediately: a robots.txt
    refusal and a bot wall. Both are a site saying no, and counting to five
    before believing it would mean four more unwelcome requests.
    """
    now = now or datetime.now(UTC)
    immediate = error.error_class in {ErrorClass.ROBOTS_DISALLOWED, ErrorClass.BLOCKED}

    targets = session.execute(
        select(MonitorTarget).where(MonitorTarget.id.in_(target_ids))
    ).scalars().all()

    for target in targets:
        target.last_failure_at = now
        target.consecutive_failures += 1

        was_probing = target.circuit_state == CircuitState.HALF_OPEN
        if immediate or was_probing or target.consecutive_failures >= FAILURE_THRESHOLD:
            if target.circuit_state != CircuitState.OPEN:
                log.warning(
                    "circuit_opened",
                    target_id=target.id,
                    error_class=str(error.error_class),
                    consecutive_failures=target.consecutive_failures,
                )
            target.circuit_state = CircuitState.OPEN
            target.circuit_opened_at = now


def record_error(
    session: Session,
    *,
    error: FetchError,
    hotel_id: int | None,
    source_id: int | None,
    target_id: int | None,
    check_run_id: str | None,
    now: datetime | None = None,
) -> MonitoringError:
    """Write the row the Health tab reads.

    The screenshot and HTML paths are lifted out of the error context because
    they are what turn "the adapter broke" into a ten-minute fix.
    """
    context = scrub(dict(error.context or {}))
    row = MonitoringError(
        monitor_target_id=target_id,
        hotel_id=hotel_id,
        source_id=source_id,
        check_run_id=check_run_id,
        occurred_at=now or datetime.now(UTC),
        error_class=error.error_class,
        is_transient=error.is_transient,
        message=str(error)[:4000],
        context=context or None,
        screenshot_path=context.get("screenshot_path"),
        html_path=context.get("html_path"),
    )
    session.add(row)
    return row


#: How long a read of the stored defaults is reused, in seconds.
#:
#: These are consulted once per offer, so reading the row every time would put
#: a query in the hottest loop in the system for a value that changes when
#: somebody edits a form. A minute is short enough that a change takes effect
#: while the person who made it is still looking at the page, and long enough
#: that a fetch of forty rooms does not make forty queries.
_DEFAULTS_TTL_SECONDS = 60.0

#: (expires_at, thresholds). Module-level, so each worker process keeps its
#: own -- there is nothing to coordinate, they all read the same row.
_cached_defaults: tuple[float, Thresholds] | None = None


def forget_stored_defaults() -> None:
    """Drop the cache. Called after a write, so an edit is not waited out."""
    global _cached_defaults
    _cached_defaults = None


def default_thresholds(settings: Settings | None = None) -> Thresholds:
    """The global alert sensitivity, for callers with no target in hand.

    Read from ``alert_defaults`` -- one row, edited from Settings -- and only
    from the environment when that table has no row yet, which is the window
    between deploying this code and running its migration. A deployment that
    tuned DEFAULT_MIN_DELTA_ABS keeps exactly that value either way: the
    migration seeds the row from the environment it finds.

    Never raises. A database that cannot be read here would otherwise take
    down a fetch that had already succeeded, to decide how sensitive an alert
    should be -- so the environment answers and the check goes on.
    """
    global _cached_defaults

    now = time.monotonic()
    if _cached_defaults is not None and _cached_defaults[0] > now:
        return _cached_defaults[1]

    settings = settings or get_settings()
    thresholds = Thresholds(
        min_delta_abs=Decimal(str(settings.default_min_delta_abs)),
        min_delta_pct=Decimal(str(settings.default_min_delta_pct)),
        confirm_checks=settings.default_confirm_checks,
    )

    try:
        from app.db.models import AlertDefaults
        from app.db.session import sync_session

        with sync_session() as session:
            row = session.get(AlertDefaults, 1)
            if row is not None:
                thresholds = Thresholds(
                    min_delta_abs=row.min_delta_abs,
                    min_delta_pct=row.min_delta_pct,
                    confirm_checks=row.confirm_checks,
                )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.warning("alert_defaults_unreadable", error=str(exc)[:200])

    _cached_defaults = (now + _DEFAULTS_TTL_SECONDS, thresholds)
    return thresholds


def build_thresholds(target: MonitorTarget, settings: Settings | None = None) -> Thresholds:
    """Per-target alert sensitivity, falling back to the global defaults.

    Sensitivity belongs on the target because a ₹200 move matters at a ₹2,000
    hotel and is noise at a ₹20,000 one. A target that states nothing inherits
    the deployment default, which is editable on the Settings page -- so the
    common case needs no per-hotel decision at all.
    """
    fallback = default_thresholds(settings)
    return Thresholds(
        min_delta_abs=(
            target.min_delta_abs
            if target.min_delta_abs is not None
            else fallback.min_delta_abs
        ),
        min_delta_pct=(
            target.min_delta_pct
            if target.min_delta_pct is not None
            else fallback.min_delta_pct
        ),
        confirm_checks=(
            target.confirm_checks
            if target.confirm_checks is not None
            else fallback.confirm_checks
        ),
    )


def stale_targets(session: Session, now: datetime | None = None) -> list[MonitorTarget]:
    """Enabled targets with no success in three intervals.

    Alerting on silence, not only on errors: the failure mode that actually
    costs money is the one where the dashboard looks fine and the prices are
    simply frozen.
    """
    now = now or datetime.now(UTC)
    targets = session.execute(
        select(MonitorTarget).where(MonitorTarget.is_enabled.is_(True))
    ).scalars().all()
    return [t for t in targets if t.is_stale(now)]
