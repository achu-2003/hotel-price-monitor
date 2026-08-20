"""The scheduler sweep and the price fetch.

Two tasks, and a hard rule between them: **a failure inside ``fetch_prices``
never escapes it**. The blast radius of anything going wrong at one hotel is
that hotel. It writes a ``monitoring_errors`` row, marks its check run failed,
and returns normally — beat keeps ticking and the other twenty-nine hotels are
untouched.

The transaction discipline is equally deliberate. The database session is
opened, used, and closed BEFORE the fetch, and opened again after it. A browser
fetch takes 20-40 seconds; holding a transaction across it would pin a
connection per in-flight hotel and Postgres would start refusing new ones long
before the browsers ran out.
"""
from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
from typing import Any

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.adapters import registry
from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.config import get_settings
from app.core.errors import (
    ErrorClass,
    FetchError,
    SchemaDriftError,
    TimeoutError_,
    classify,
)
from app.core.logging import get_logger
from app.core.ratelimit import (
    LockNotAcquired,
    dispatch_lock,
    effective_rate_per_min,
    penalise_source,
    take_token,
)
from app.db.models import CheckRun, CheckRunStatus, MonitorTarget, PriceBasis
from app.db.session import sync_session
from app.services import monitoring
from app.services.dates import StayWindow
from app.services.ingest import IngestContext, ingest_fetch_result
from app.services.monitoring import DueGroup

log = get_logger("tasks.fetch")

#: How long the per-group lock is held. Twice the task time limit, so a worker
#: that is killed mid-fetch cannot leave a lock that outlives the next window.
_LOCK_TTL_SECONDS = 600

#: Backoff per attempt, seconds, before jitter. Long on purpose: a site that
#: just failed is not helped by three requests in thirty seconds.
_BACKOFF = (30, 120, 480)


# -- serialisation ---------------------------------------------------
# Groups cross the broker as plain JSON. Sending ORM objects (or pickles)
# would mean a worker running older code could not deserialise a message from
# a newer scheduler — a failure that only shows up mid-deploy.
def group_to_payload(group: DueGroup) -> dict[str, Any]:
    return {
        "hotel_id": group.hotel_id,
        "hotel_name": group.hotel_name,
        "hotel_source_id": group.hotel_source_id,
        "source_id": group.source_id,
        "adapter_key": group.adapter_key,
        "url": group.url,
        "external_id": group.external_id,
        "currency": group.currency,
        "adapter_config": group.adapter_config,
        "check_in": group.stay.check_in.isoformat(),
        "check_out": group.stay.check_out.isoformat(),
        "adults": group.adults,
        "children": group.children,
        "rooms": group.rooms,
        "meal_plan_filter": group.meal_plan_filter,
        "target_ids": list(group.target_ids),
        "rate_limit_per_min": group.rate_limit_per_min,
    }


def _stay_from_payload(payload: dict[str, Any]) -> StayWindow:
    return StayWindow(
        check_in=datetime.fromisoformat(payload["check_in"]).date(),
        check_out=datetime.fromisoformat(payload["check_out"]).date(),
    )


# -- the sweep -------------------------------------------------------
@shared_task(name="monitor.dispatch_due_checks", ignore_result=True)
def dispatch_due_checks() -> dict[str, int]:
    """Find what is due and enqueue it. Runs every 60 seconds.

    ``next_run_at`` is advanced here rather than when the fetch completes: a
    task that hangs would otherwise leave its target permanently due and every
    subsequent sweep would enqueue it again.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    enqueued = 0

    with sync_session() as session:
        groups = monitoring.find_due_groups(session, now)
        for group in groups:
            countdown = random.uniform(0, settings.dispatch_jitter_seconds)
            fetch_prices.apply_async(
                args=[group_to_payload(group)],
                queue=group.queue,
                countdown=countdown,
                # Nothing useful comes of running a check that has been sitting
                # in the queue longer than the interval it belongs to.
                expires=now.timestamp() + 1800,
            )
            monitoring.schedule_next_run(session, group.target_ids, now)
            enqueued += 1

    if enqueued:
        log.info("dispatched", groups=enqueued)
    return {"groups": enqueued}


# -- the fetch -------------------------------------------------------
@shared_task(
    bind=True,
    name="fetch.prices",
    max_retries=3,
    ignore_result=True,
)
def fetch_prices(self, payload: dict[str, Any], triggered_by: str = "scheduler",
                 check_run_id: str | None = None) -> dict[str, Any]:
    """Fetch one hotel's prices for one stay window and ingest the result."""
    stay = _stay_from_payload(payload)
    target_ids: list[int] = payload["target_ids"]
    check_run_id = check_run_id or str(uuid.uuid4())
    started = datetime.now(UTC)

    # Bound once so every line from this fetch carries the hotel and run id.
    # Correlating a failure to a check run is otherwise guesswork when thirty
    # hotels are logging into the same stream.
    logger = log.bind(
        hotel=payload["hotel_name"],
        hotel_source_id=payload["hotel_source_id"],
        check_in=payload["check_in"],
        check_run_id=check_run_id,
    )

    try:
        with dispatch_lock(f"lock:{_lock_name(payload)}", _LOCK_TTL_SECONDS):
            return _run_fetch(self, payload, stay, target_ids, check_run_id,
                              triggered_by, started, logger)
    except LockNotAcquired:
        # The previous run of this exact group is still going. Skipping is
        # correct: two browsers at one hotel is both impolite and pointless.
        _write_check_run(
            check_run_id, payload, stay, started, CheckRunStatus.SKIPPED,
            triggered_by=triggered_by,
            error_summary="A previous check for this hotel and stay window was still running.",
        )
        log.info("fetch_skipped_locked", hotel=payload["hotel_name"])
        return {"status": "skipped", "check_run_id": check_run_id}


def _run_fetch(
    task,
    payload: dict[str, Any],
    stay: StayWindow,
    target_ids: list[int],
    check_run_id: str,
    triggered_by: str,
    started: datetime,
    logger,
) -> dict[str, Any]:
    settings = get_settings()

    # Politeness budget first: no point opening a browser we are not allowed
    # to use yet.
    rate = effective_rate_per_min(payload["source_id"], payload["rate_limit_per_min"])
    verdict = take_token(payload["source_id"], rate)
    if not verdict.allowed:
        logger.info("rate_limited_deferring", wait_s=round(verdict.retry_after_seconds, 1))
        raise task.retry(countdown=verdict.retry_after_seconds, max_retries=5)

    _write_check_run(check_run_id, payload, stay, started, CheckRunStatus.RUNNING,
                     triggered_by=triggered_by)

    context = FetchContext(
        hotel_source_id=payload["hotel_source_id"],
        hotel_name=payload["hotel_name"],
        url=payload["url"],
        external_id=payload["external_id"],
        stay=stay,
        adults=payload["adults"],
        children=payload["children"],
        rooms=payload["rooms"],
        currency=payload["currency"],
        locale=settings.browser_locale,
        timezone=settings.browser_timezone,
        config=payload["adapter_config"],
        check_run_id=check_run_id,
    )

    # No database session is held across this call. It is the slow part.
    try:
        adapter = registry.get_adapter(payload["adapter_key"])
        result: FetchResult = adapter.fetch(context)
    except SoftTimeLimitExceeded:
        # Celery raises this in the task's own thread at the soft limit. Caught
        # separately from the generic handler because it is not an exception
        # the adapter raised, and classify() would file it as unknown.
        return _handle_failure(
            task, TimeoutError_("The fetch exceeded its time limit"),
            payload, stay, target_ids, check_run_id, started, triggered_by, logger,
        )
    except Exception as exc:  # noqa: BLE001 - classified, never propagated
        return _handle_failure(
            task, classify(exc), payload, stay, target_ids, check_run_id,
            started, triggered_by, logger,
        )

    return _ingest(result, payload, stay, target_ids, check_run_id, started,
                   triggered_by, logger)


def _ingest(
    result: FetchResult,
    payload: dict[str, Any],
    stay: StayWindow,
    target_ids: list[int],
    check_run_id: str,
    started: datetime,
    triggered_by: str,
    logger,
) -> dict[str, Any]:
    """Persist the offers and hand any confirmed changes to the notify queue."""
    now = datetime.now(UTC)

    with sync_session() as session:
        target = session.execute(
            select(MonitorTarget).where(MonitorTarget.id == target_ids[0])
        ).scalar_one_or_none()

        ctx = IngestContext(
            hotel_id=payload["hotel_id"],
            source_id=payload["source_id"],
            hotel_source_id=payload["hotel_source_id"],
            stay=stay,
            adults=payload["adults"],
            children=payload["children"],
            currency=payload["currency"],
            # Configured, not assumed: see Settings.price_basis. A series
            # recorded on the other basis is rebased by the ingest layer
            # rather than reported as a price move.
            price_basis=PriceBasis(get_settings().price_basis),
            # The target can be deleted between dispatch and ingest. The
            # observations are still worth keeping, so fall back to the global
            # sensitivity rather than discarding the fetch.
            thresholds=(
                monitoring.build_thresholds(target)
                if target is not None
                else monitoring.default_thresholds()
            ),
            checked_at=now,
            check_run_id=check_run_id,
            meal_plan_filter=payload.get("meal_plan_filter"),
        )
        summary = ingest_fetch_result(session, result, ctx)

        # A fetch that read several rooms and could only file one of them is a
        # SUCCESS by every measure this task has -- the page loaded, the
        # selectors matched, offers were found and none went unmatched -- and
        # it is still wrong. It means the room_name selector has landed on
        # something every card shares, so the hotel's rooms all arrive with one
        # identity and the pipeline keeps the first.
        #
        # That happened to a six-room property whose name selector found an
        # amenity chip reading "King Size Bed" on all six cards. Nothing
        # failed, nothing was retried, and the dashboard showed one room for
        # weeks. Recorded here so the next occurrence is a line on Attention
        # rather than a discrepancy somebody happens to notice.
        if summary.offers_collapsed:
            monitoring.record_error(
                session,
                error=SchemaDriftError(
                    f"{summary.offers_collapsed} of {summary.offers_seen} offers "
                    f"shared an identity with another offer in the same fetch and "
                    f"were dropped, so this hotel is being monitored as "
                    f"{summary.offers_seen - summary.offers_collapsed} room(s) "
                    f"instead of {summary.offers_seen}. The room_name selector is "
                    f"almost certainly reading a label every room card shares.",
                    context={
                        "names_seen": summary.collapsed_names[:8],
                        "hotel_source_id": payload["hotel_source_id"],
                        "selectors": (payload.get("adapter_config") or {}).get("selectors"),
                    },
                ),
                hotel_id=payload["hotel_id"],
                source_id=payload["source_id"],
                target_id=target_ids[0],
                check_run_id=check_run_id,
                now=now,
            )

        monitoring.record_success(session, target_ids, now)
        _update_check_run(
            session,
            check_run_id,
            status=CheckRunStatus.SUCCESS,
            finished_at=now,
            duration_ms=int((now - started).total_seconds() * 1000),
            offers_found=summary.offers_seen,
            offers_unmatched=summary.offers_unmatched,
            changes_detected=summary.changes_detected,
        )
        change_ids = list(summary.change_ids)

    logger.info(
        "fetch_complete",
        offers=summary.offers_seen,
        matched=summary.offers_matched,
        unmatched=summary.offers_unmatched,
        changes=len(change_ids),
    )

    # Enqueued after the transaction, for the same reason the notify hand-off
    # is: the repair reads this source's row, and must never race the write
    # that told it to run.
    if summary.offers_collapsed:
        _request_repair(
            payload, stay, reason="offers_collapsed", logger=logger,
            # The labels that collapsed. The repair needs them to tell the rooms
            # the broken config invented from the hotel's real ones.
            collapsed_names=summary.collapsed_names,
        )

    if change_ids:
        # Enqueued only after the transaction has committed, so the notify
        # worker can never read a change row that does not exist yet.
        from app.workers.tasks_notify import dispatch_changes

        dispatch_changes.apply_async(
            args=[change_ids], queue="notify",
            countdown=get_settings().digest_window_seconds,
        )

    return {
        "status": "success",
        "check_run_id": check_run_id,
        "offers": summary.offers_seen,
        "changes": len(change_ids),
    }


def _request_repair(
    payload: dict[str, Any],
    stay: StayWindow,
    *,
    reason: str,
    logger,
    collapsed_names: list[str] | None = None,
) -> None:
    """Ask discovery to re-derive this source's config.

    Advisory, not obligatory: the task decides for itself whether it is allowed
    to run (see app/services/rediscovery.py). Everything here is wrapped
    because a broker that is momentarily unreachable must not turn a successful
    fetch into a failed one — the price data is already committed, and the
    alert on Attention stands whether or not this hand-off lands.
    """
    if not get_settings().auto_rediscovery_enabled:
        return
    try:
        from app.workers.tasks_repair import rediscover_source

        rediscover_source.apply_async(
            args=[payload["hotel_source_id"]],
            kwargs={
                # The same stay window the fetch used, so discovery inspects
                # the page the fetch actually read rather than a default one
                # that might legitimately show different rooms.
                "check_in": stay.check_in.isoformat(),
                "check_out": stay.check_out.isoformat(),
                "adults": payload["adults"],
                "children": payload["children"],
                "reason": reason,
                "collapsed_names": list(collapsed_names or []),
            },
            queue="browser",
        )
        logger.info("repair_requested", reason=reason)
    except Exception as exc:  # noqa: BLE001 - never fails the fetch
        logger.warning("repair_request_failed", error=str(exc)[:200])


def _handle_failure(
    task,
    error: FetchError,
    payload: dict[str, Any],
    stay: StayWindow,
    target_ids: list[int],
    check_run_id: str,
    started: datetime,
    triggered_by: str,
    logger,
) -> dict[str, Any]:
    """Classify, record, and decide whether this is worth trying again.

    Everything is recorded before the retry decision, so an error that is
    ultimately retried successfully still leaves a trail — that trail is how a
    site that is slowly degrading gets noticed before it fails outright.
    """
    now = datetime.now(UTC)
    attempt = task.request.retries

    if error.error_class == ErrorClass.RATE_LIMITED:
        penalise_source(payload["source_id"])

    with sync_session() as session:
        monitoring.record_error(
            session,
            error=error,
            hotel_id=payload["hotel_id"],
            source_id=payload["source_id"],
            target_id=target_ids[0] if target_ids else None,
            check_run_id=check_run_id,
            now=now,
        )

        will_retry = error.is_transient and attempt < error.max_retries
        if not will_retry:
            monitoring.record_failure(session, target_ids, error, now)

        _update_check_run(
            session,
            check_run_id,
            status=CheckRunStatus.FAILED,
            finished_at=now,
            duration_ms=int((now - started).total_seconds() * 1000),
            error_summary=f"[{error.error_class}] {str(error)[:800]}",
        )

    logger.warning(
        "fetch_failed",
        error_class=str(error.error_class),
        transient=error.is_transient,
        attempt=attempt,
        message=str(error)[:300],
    )

    if error.is_transient and attempt < error.max_retries:
        raise task.retry(exc=error, countdown=_retry_delay(error, attempt))

    # The other way a redesign shows up. Where a collapsed room list means the
    # selectors still match something wrong, drift means they match nothing at
    # all -- the adapter refused to guess and said so. Both are repaired the
    # same way, and only these two are: a timeout or a block means the page was
    # never read, so re-running discovery against it would just add load to a
    # site already refusing us.
    if error.error_class == ErrorClass.PARSE_SCHEMA_DRIFT:
        _request_repair(payload, stay, reason="schema_drift", logger=logger)

    # Permanent, or out of retries. Return normally: this hotel is done for
    # this cycle, and nothing else should be affected by it.
    return {"status": "failed", "check_run_id": check_run_id,
            "error_class": str(error.error_class)}


def _retry_delay(error: FetchError, attempt: int) -> float:
    """Exponential backoff with jitter, honouring ``Retry-After`` when given.

    The jitter is not cosmetic: without it, thirty hotels failing on the same
    network blip retry in the same second and recreate the blip.
    """
    if error.error_class == ErrorClass.RATE_LIMITED:
        wait = getattr(error, "retry_after_seconds", None)
        if wait:
            return float(wait) + random.uniform(0, 30)
    base = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
    return base + random.uniform(0, base * 0.25)


# -- manual entry ----------------------------------------------------
@shared_task(name="fetch.record_manual_offers", ignore_result=True)
def record_manual_offers(
    payload: dict[str, Any], check_run_id: str, triggered_by: str = "manual"
) -> dict[str, Any]:
    """Ingest prices an operator typed into the dashboard.

    Runs through the identical pipeline as a scraped fetch — same offer key,
    same comparison, same debounce, same notifications. A hotel that has to be
    tracked by hand therefore produces history indistinguishable from an
    automated one, which is what makes manual entry a real fallback rather
    than a spreadsheet bolted onto the side.

    The room type is supplied by the operator, so the room-matching step is
    skipped: a human choosing from a list is already the highest-confidence
    mapping this system recognises.
    """
    from app.db.models import Hotel, HotelSource, RoomType

    now = datetime.now(UTC)
    offers = payload["offers"]
    changes: list[int] = []

    with sync_session() as session:
        hotel_source = session.get(HotelSource, payload["hotel_source_id"])
        if hotel_source is None:
            log.warning("manual_entry_unknown_hotel_source", payload_keys=sorted(payload))
            return {"status": "failed", "reason": "unknown hotel_source"}

        hotel = session.get(Hotel, hotel_source.hotel_id)
        thresholds = monitoring.default_thresholds()
        offers_written = 0

        for entry in offers:
            room = session.get(RoomType, entry["room_type_id"])
            if room is None or room.hotel_id != hotel_source.hotel_id:
                log.warning(
                    "manual_entry_bad_room",
                    room_type_id=entry["room_type_id"],
                    hotel_id=hotel_source.hotel_id,
                )
                continue

            stay = StayWindow(
                check_in=datetime.fromisoformat(entry["check_in"]).date(),
                check_out=datetime.fromisoformat(entry["check_out"]).date(),
            )
            offer = NormalizedOffer(
                raw_room_name=room.name,
                price_inclusive=_decimal(entry.get("price_inclusive")),
                price_exclusive=_decimal(entry.get("price_exclusive")),
                taxes_fees=_decimal(entry.get("taxes_fees")),
                currency=entry.get("currency") or hotel_source.currency,
                meal_plan=entry.get("meal_plan"),
                refundable=entry.get("refundable"),
                is_available=entry.get("is_available", True),
                raw_payload={"source": "manual_entry", "entered_by": triggered_by},
            )

            ctx = IngestContext(
                hotel_id=hotel_source.hotel_id,
                source_id=hotel_source.source_id,
                hotel_source_id=hotel_source.id,
                stay=stay,
                adults=entry.get("adults", 2),
                children=entry.get("children", 0),
                currency=offer.currency,
                price_basis=PriceBasis(get_settings().price_basis),
                thresholds=thresholds,
                checked_at=now,
                check_run_id=check_run_id,
            )
            # One offer at a time: each carries its own stay window, and the
            # disappearance sweep is scoped to one window. Batching windows
            # together would mark rooms of OTHER dates as no longer listed.
            summary = ingest_fetch_result(
                session,
                FetchResult(offers=[offer], sold_out_detected=not offer.is_available),
                ctx,
            )
            changes.extend(summary.change_ids)
            offers_written += 1

        _update_check_run(
            session,
            check_run_id,
            status=CheckRunStatus.SUCCESS,
            finished_at=now,
            offers_found=offers_written,
            changes_detected=len(changes),
        )

    if changes:
        from app.workers.tasks_notify import dispatch_changes

        dispatch_changes.apply_async(args=[changes], queue="notify")

    log.info(
        "manual_entry_recorded",
        hotel=hotel.name if hotel else None,
        offers=offers_written,
        changes=len(changes),
    )
    return {"status": "success", "offers": offers_written, "changes": len(changes)}


def _decimal(value):
    from decimal import Decimal

    return None if value is None else Decimal(str(value))


# -- check_run bookkeeping -------------------------------------------
def _write_check_run(
    check_run_id: str,
    payload: dict[str, Any],
    stay: StayWindow,
    started: datetime,
    status: CheckRunStatus,
    *,
    triggered_by: str,
    error_summary: str | None = None,
) -> None:
    """Insert (or leave alone) the row the dashboard polls.

    The API creates this row before returning 202 for a manual run, so this has
    to tolerate it already existing.
    """
    with sync_session() as session:
        existing = session.get(CheckRun, check_run_id)
        if existing is not None:
            existing.status = status
            if error_summary:
                existing.error_summary = error_summary
            return

        session.add(
            CheckRun(
                id=check_run_id,
                monitor_target_id=payload["target_ids"][0] if payload["target_ids"] else None,
                triggered_by=triggered_by,
                started_at=started,
                status=status,
                check_in=stay.check_in,
                check_out=stay.check_out,
                error_summary=error_summary,
            )
        )


def _update_check_run(session, check_run_id: str, **fields: Any) -> None:
    run = session.get(CheckRun, check_run_id)
    if run is None:
        return
    for key, value in fields.items():
        setattr(run, key, value)


def _lock_name(payload: dict[str, Any]) -> str:
    return (
        f"fetch:{payload['hotel_source_id']}:{payload['check_in']}"
        f":{payload['check_out']}:{payload['adults']}:{payload['children']}"
    )
