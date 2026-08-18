"""Celery application: queues, routing, beat schedule, and worker lifecycle.

QUEUE SEPARATION
================
Three queues, because the work has three completely different resource
profiles and mixing them means the heaviest one starves the others:

``browser``  ~400MB of Chromium and 20-40s per task. Concurrency 3.
``http``     ~5MB and under a second. Concurrency 8.
``notify``   network-bound, must stay responsive even while browsers churn.

A single shared queue would let thirty queued page loads delay an alert that
was ready to send.

WHY ONE BEAT ENTRY
==================
Beat fires ``dispatch_due_checks`` every 60 seconds and nothing else. Per-hotel
intervals live in ``monitor_targets.interval_minutes``, so changing a hotel's
schedule is a dashboard edit rather than a config change and a restart. Thirty
beat entries would also mean thirty places to get the timezone wrong.
"""
from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging, worker_process_shutdown

from app.config import get_settings
from app.core.logging import configure_logging, get_logger

log = get_logger("celery")
settings = get_settings()

celery_app = Celery("hotel_price_monitor")

celery_app.conf.update(
    broker_url=settings.celery_broker_url,
    result_backend=settings.celery_result_backend,
    # JSON only: pickle would let anything with write access to Redis execute
    # code in a worker.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.timezone,
    enable_utc=True,
    # Tasks are re-delivered if a worker dies mid-fetch. Safe because every
    # write in the pipeline is idempotent on (offer_key, checked_at).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One task in flight per worker child. The default of four would have a
    # browser worker sitting on three page loads it cannot start while another
    # worker idles.
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # A browser fetch that has not finished in 5 minutes is stuck, not slow.
    task_soft_time_limit=300,
    task_time_limit=420,
    result_expires=86_400,
    broker_connection_retry_on_startup=True,
    task_default_queue="http",
    task_queues=None,
    task_routes={
        "fetch.prices": {"queue": "browser"},
        "fetch.prices_http": {"queue": "http"},
        "notify.dispatch_changes": {"queue": "notify"},
        "notify.send": {"queue": "notify"},
        "notify.release_quiet_hours": {"queue": "notify"},
        "monitor.dispatch_due_checks": {"queue": "http"},
        "maintenance.*": {"queue": "http"},
    },
    beat_schedule={
        "dispatch-due-checks": {
            "task": "monitor.dispatch_due_checks",
            "schedule": 60.0,
            # Beat can fall behind after a restart; replaying a backlog of
            # dispatch sweeps would enqueue the same work several times.
            "options": {"expires": 55},
        },
        "release-quiet-hours": {
            "task": "notify.release_quiet_hours",
            "schedule": 300.0,
            "options": {"expires": 280},
        },
        "sweep-stale-targets": {
            "task": "maintenance.alert_on_silence",
            "schedule": 900.0,
        },
        "prune-artifacts": {
            "task": "maintenance.prune_artifacts",
            "schedule": 86_400.0,
        },
        "ensure-partitions": {
            "task": "maintenance.ensure_partitions",
            "schedule": 86_400.0,
        },
    },
)

# Import for their side effect of registering tasks. Kept at the bottom so the
# app object exists before any task decorator runs.
celery_app.autodiscover_tasks(
    [
        "app.workers.tasks_fetch",
        "app.workers.tasks_notify",
        "app.workers.tasks_maintenance",
    ],
    force=True,
)


@setup_logging.connect
def _configure_celery_logging(**_kwargs) -> None:
    """Take over Celery's logging so worker output is structured and redacted.

    Without this, Celery installs its own handlers and task logs bypass the
    redaction processor entirely — which is exactly where a credential in an
    exception message would surface.
    """
    configure_logging()


@worker_process_shutdown.connect
def _close_browser(**_kwargs) -> None:
    """Shut Chromium down when a worker child is recycled.

    ``--max-tasks-per-child`` recycles browser workers regularly; without this
    each recycle leaks a browser process until the box runs out of memory.
    """
    try:
        from app.adapters.playwright_base import browser_pool

        browser_pool.close()
    except Exception as exc:  # noqa: BLE001 - shutdown must not raise
        log.warning("browser_shutdown_failed", error=str(exc))
