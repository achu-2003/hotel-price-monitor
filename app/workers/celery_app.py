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
from celery.schedules import crontab
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
        # Drives a real browser, so it belongs with the fetches. Routed here as
        # well as at the call site: task_default_queue is "http", and a caller
        # that forgot the keyword would land Chromium on the light worker --
        # concurrency 8, no shm_size, no memory headroom -- which fails as an
        # out-of-memory kill rather than as anything that names the cause.
        "repair.rediscover_source": {"queue": "browser"},
        # Same reasoning, other caller: attaching a hotel on an unrecognised
        # engine inspects the page in a browser, and the API image has none.
        "discover.inspect_url": {"queue": "browser"},
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
        # The one alarm that works when THIS process is the thing that failed.
        # Five minutes so a watchdog with a ten-minute grace period reports a
        # genuine outage rather than a slow tick.
        "heartbeat": {
            "task": "maintenance.heartbeat",
            "schedule": 300.0,
            "options": {"expires": 280},
        },
        "prune-artifacts": {
            "task": "maintenance.prune_artifacts",
            "schedule": 86_400.0,
        },
        # Weekly, not daily. Dropping a partition is instant and irreversible,
        # so the sweep runs seldom enough that a wrong RETENTION_MONTHS is
        # noticed in the logs before it has eaten a second month of history.
        "retention-sweep": {
            "task": "maintenance.retention_sweep",
            "schedule": 604_800.0,
        },
        "ensure-partitions": {
            "task": "maintenance.ensure_partitions",
            "schedule": 86_400.0,
        },
        # Once a month, on the 1st, at half past three in the morning.
        #
        # crontab rather than a 30-day interval: an interval counts from the
        # last time BEAT started, so a worker restarted every few weeks --
        # which is how this deployment gets its updates -- would push the
        # sweep forever into a future it never reaches, and the tables it
        # exists to bound would grow without anybody being told. A calendar
        # date cannot be postponed by a restart.
        #
        # 03:30 because the hour is the quietest for the sites being checked
        # and the half hour keeps it off the top-of-hour tick that every
        # other schedule here shares.
        "clean-history": {
            "task": "maintenance.clean_history",
            "schedule": crontab(day_of_month="1", hour=3, minute=30),
        },
        # The only route by which a scanner fix reaches a hotel that is
        # silently wrong -- one reading the property next door's prices, say,
        # where every check succeeds and nothing ever asks for a repair. See
        # maintenance.sweep_stale_configs.
        #
        # Hourly, and it hands out five at a time. Nothing here is urgent: the
        # configs it finds have been wrong for as long as it took to notice,
        # and each repair drives a browser against someone else's site.
        "sweep-stale-configs": {
            "task": "maintenance.sweep_stale_configs",
            "schedule": 3_600.0,
            "options": {"expires": 3_500},
        },
    },
)

# Make this the app that bare @shared_task decorators bind to. Without it, a
# task module imported before this one (the API does exactly that to queue a
# manual run) attaches to Celery's default app, whose broker is
# amqp://localhost:5672 — and apply_async then fails with "connection refused"
# while Redis is demonstrably healthy.
celery_app.set_default()

# Import for their side effect of registering tasks. Kept at the bottom so the
# app object exists, and is the default, before any task decorator runs.
celery_app.autodiscover_tasks(
    [
        "app.workers.tasks_fetch",
        "app.workers.tasks_notify",
        "app.workers.tasks_maintenance",
        "app.workers.tasks_repair",
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
