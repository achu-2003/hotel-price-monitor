"""Housekeeping that keeps the system honest while nobody is watching.

Five jobs, each addressing a way this system fails quietly rather than loudly:

``ensure_partitions``   next month's partition must exist BEFORE the first of
                        the month, or every insert fails at midnight
``alert_on_silence``    a target that stopped succeeding without erroring
``prune_artifacts``     screenshots and HTML fill a disk otherwise
``retention_sweep``     drop observation partitions past the retention window
``sweep_stale_configs`` a config the current scanner would not have written
"""
from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path

from celery import shared_task
from sqlalchemy import select, text

from app.config import get_settings
from app.core.logging import get_logger
from app.db.session import sync_session
from app.services import monitoring
from app.services.dates import local_today, resolve_stay_window
from app.services.rediscovery import DISCOVERY_VERSION, needs_rescan

log = get_logger("tasks.maintenance")

#: Keep this many months of raw observations. Two years of half-hourly checks
#: on thirty hotels is roughly 12M rows — comfortable, and worth having when
#: someone asks what last year's Diwali rates looked like.
RETENTION_MONTHS = 24


@shared_task(name="maintenance.ensure_partitions", ignore_result=True)
def ensure_partitions(months_ahead: int = 3) -> dict[str, int]:
    """Create the next few monthly partitions of ``price_observations``.

    Runs daily and creates several months ahead, not one. A single month of
    headroom means a job that fails silently for four weeks takes the whole
    system down at midnight on the first — and always on a weekend.

    Delegates to ``create_price_observation_partition``, the function the
    initial migration installs. The partition naming and the range bounds are
    defined once, in SQL, so this task and the migration cannot disagree about
    where a row belongs — and the parameter is bound, never interpolated.
    """
    created = 0
    today = date.today()

    with sync_session() as session:
        for offset in range(months_ahead + 1):
            start = _add_months(today.replace(day=1), offset)
            session.execute(
                text("SELECT create_price_observation_partition(:month)"),
                {"month": start},
            )
            created += 1

    log.info("partitions_ensured", months=created)
    return {"partitions": created}


@shared_task(name="maintenance.retention_sweep", ignore_result=True)
def retention_sweep(keep_months: int = RETENTION_MONTHS) -> dict[str, int]:
    """Drop observation partitions older than the retention window.

    ``DROP TABLE`` on a partition is instant and reclaims the space
    immediately. The equivalent ``DELETE`` would take minutes, lock rows, and
    leave the space to be reclaimed by a vacuum that may never come. This is
    the payoff for partitioning from day one.
    """
    cutoff = _add_months(date.today().replace(day=1), -keep_months)
    dropped = 0

    with sync_session() as session:
        rows = session.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'price_observations'"
            )
        ).scalars().all()

        for name in rows:
            partition_month = _month_from_name(name)
            if partition_month is not None and partition_month < cutoff:
                session.execute(text(f"DROP TABLE IF EXISTS {name}"))
                log.info("partition_dropped", partition=name)
                dropped += 1

    return {"dropped": dropped}


@shared_task(name="maintenance.alert_on_silence", ignore_result=True)
def alert_on_silence() -> dict[str, int]:
    """Find targets that have gone quiet without failing.

    The failure mode that actually costs money is not an error — it is a check
    that stopped happening while the dashboard kept showing yesterday's prices
    as though they were current. An erroring target is visible; a silent one is
    not, which is why silence gets its own alarm.
    """
    from app.workers.tasks_notify import notify_ops, send_notification

    now = datetime.now(UTC)
    queued: list[int] = []

    with sync_session() as session:
        stale = monitoring.stale_targets(session, now)
        for target in stale:
            log.warning(
                "target_stale",
                target_id=target.id,
                hotel_source_id=target.hotel_source_id,
                interval_minutes=target.interval_minutes,
                last_success_at=(
                    target.last_success_at.isoformat() if target.last_success_at else None
                ),
                circuit_state=str(target.circuit_state),
            )

        if stale:
            # One message per person per day per distinct set of stale targets.
            # This task runs every fifteen minutes and an outage lasts hours;
            # without the date-and-membership token it would send ninety-six
            # identical emails and teach the recipient to ignore all of them.
            # A *new* target going quiet changes the set, and does interrupt.
            token = f"stale|{now:%Y-%m-%d}|{','.join(str(t.id) for t in sorted(stale, key=lambda t: t.id))}"
            subject, body = _silence_message(session, stale, now)
            queued = notify_ops(session, subject=subject, body=body, token=token, now=now)

    # After the commit: a worker must never be handed an id that is still
    # sitting in an uncommitted transaction.
    for notification_id in queued:
        send_notification.apply_async(args=[notification_id], queue="notify")

    return {"stale": len(stale), "alerted": len(queued)}


def _silence_message(session, stale, now: datetime) -> tuple[str, str]:
    """Name the hotels, not the target ids.

    An alert that says "targets 11, 14 and 19 are stale" requires the reader to
    open the dashboard before they know whether to care. Naming the property
    and how long it has been quiet lets them decide from the notification.
    """
    from app.db.models import Hotel, HotelSource

    lines = []
    for target in sorted(stale, key=lambda t: t.id):
        name = f"target {target.id}"
        link = session.get(HotelSource, target.hotel_source_id)
        if link is not None:
            hotel = session.get(Hotel, link.hotel_id)
            if hotel is not None:
                name = hotel.name

        if target.last_success_at is None:
            age = "never succeeded"
        else:
            hours = (now - target.last_success_at).total_seconds() / 3600
            age = f"last succeeded {hours:.0f}h ago" if hours >= 1 else "last succeeded <1h ago"

        lines.append(f"  - {name} — {age} (checks every {target.interval_minutes}m)")

    count = len(stale)
    subject = (
        f"Price monitoring has gone quiet on "
        f"{count} {'source' if count == 1 else 'sources'}"
    )
    body = (
        "These monitor targets are enabled but have not succeeded in three "
        "consecutive intervals.\n\n"
        + "\n".join(lines)
        + "\n\nThe dashboard is still showing their last known prices, which "
        "are now stale. Check the Attention page for the underlying error.\n"
    )
    return subject, body


@shared_task(name="maintenance.prune_artifacts", ignore_result=True)
def prune_artifacts() -> dict[str, int]:
    """Delete failure screenshots and HTML past their retention.

    Artifacts are what make a broken selector a ten-minute fix, but a
    screenshot per failure per hotel fills a disk within weeks, and a full disk
    stops Postgres — turning a cosmetic problem into an outage.
    """
    settings = get_settings()
    directory = Path(settings.artifact_dir)
    if not directory.exists():
        return {"removed": 0}

    cutoff = time.time() - settings.artifact_retention_days * 86_400
    removed = 0
    for path in directory.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the sweep
            log.warning("artifact_delete_failed", path=str(path), error=str(exc))

    if removed:
        log.info("artifacts_pruned", removed=removed)
    return {"removed": removed}


@shared_task(name="maintenance.heartbeat", ignore_result=True)
def heartbeat() -> dict[str, str]:
    """Ping an external watchdog, so somebody notices if this all stops.

    Deliberately does more than ``GET``: it touches the database first. A beat
    process that is alive but whose database is gone would otherwise keep the
    watchdog happy while nothing works, which is a worse failure than no
    monitoring at all -- it is monitoring that lies.

    Never raises. A watchdog that cannot be reached is not a reason to fill the
    error log; the watchdog's own alarm covers that case by definition.
    """
    settings = get_settings()
    if not settings.heartbeat_url:
        return {"status": "disabled"}

    try:
        with sync_session() as session:
            session.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:  # noqa: BLE001 - a sick database must not ping
        log.error("heartbeat_db_unreachable", error=str(exc))
        return {"status": "db_unreachable"}

    try:
        import httpx

        httpx.get(settings.heartbeat_url, timeout=settings.heartbeat_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("heartbeat_ping_failed", error=str(exc))
        return {"status": "ping_failed"}

    return {"status": "ok"}


# -- date helpers ----------------------------------------------------
def _add_months(anchor: date, months: int) -> date:
    """Month arithmetic on the first of a month.

    ``timedelta(days=30)`` drifts by a day or two per month and would
    eventually create a partition boundary that does not line up with a month,
    which Postgres rejects with an overlap error.
    """
    month_index = anchor.year * 12 + (anchor.month - 1) + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def _month_from_name(name: str) -> date | None:
    """``price_observations_2026_08`` -> 2026-08-01."""
    parts = name.rsplit("_", 2)
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[1]), int(parts[2]), 1)
    except ValueError:
        return None


#: How many sources one sweep may hand to the repair queue.
#:
#: Small on purpose. Each one drives a real browser against somebody else's
#: site, and this sweep is not urgent -- nothing is on fire, the hotels it
#: finds have been quietly wrong for as long as it took to notice. Five an
#: hour drains a fleet of thirty in an afternoon without ever looking like a
#: crawl, and the repair's own cooldown and budget bound it further.
STALE_CONFIG_BATCH = 5


@shared_task(name="maintenance.sweep_stale_configs", ignore_result=True)
def sweep_stale_configs(limit: int = STALE_CONFIG_BATCH) -> dict[str, int]:
    """Ask for a re-derivation of configs an older scanner wrote.

    WHY A SCANNER FIX CANNOT REACH THE HOTELS IT WAS WRITTEN FOR
    ============================================================
    Every repair in this system is triggered by a fetch that noticed something:
    offers collapsing onto one identity, a selector matching nothing, an
    endpoint gone. That covers the faults which announce themselves, and misses
    the entire class that does not.

    A hotel page carries a "similar properties" carousel. It out-scores the
    real room list on every measure the ranking has -- four repeated cards, one
    price each, four distinct names against the room's one -- so a config built
    from it monitors four COMPETITORS under this hotel's name. Downstream there
    is nothing to see: the prices are real and on the page, corroboration
    passes, no offers collapse, and every check reports success. The scan now
    demotes such a candidate by where its cards LEAD, and that fix could not
    reach a single affected hotel, because no fetch will ever ask for a repair.

    ``DISCOVERY_VERSION`` already knows which configs predate a scanner change,
    and ``may_attempt`` already lets one past a spent budget on those grounds.
    Nothing consulted it unless something else asked first. This is the asking.

    WHAT IT WILL NOT TOUCH
    ======================
    Only configs DISCOVERY wrote, identified by the ``discovery_note`` it
    stamps on them. An engine profile -- Agoda's JSON paths, aiosell's field
    map -- was written by a person against a documented payload, and re-running
    a DOM scan over one would replace curated knowledge with a guess.

    Only sources something is actually monitoring, and with the window that
    monitor uses, so discovery reads the page the fetch reads rather than a
    default one that may legitimately show different rooms.

    And only a few at a time.
    """
    from app.db.models import Hotel, HotelSource, MonitorTarget, Source
    from app.workers.tasks_repair import rediscover_source

    settings = get_settings()
    if not settings.auto_rediscovery_enabled:
        return {"stale": 0, "requested": 0}

    now = datetime.now(UTC)
    today = local_today(settings.timezone)
    requested = 0
    stale = 0

    with sync_session() as session:
        rows = session.execute(
            select(HotelSource, MonitorTarget)
            .join(MonitorTarget, MonitorTarget.hotel_source_id == HotelSource.id)
            .join(Source, Source.id == HotelSource.source_id)
            .join(Hotel, Hotel.id == HotelSource.hotel_id)
            .where(
                # The same gates find_due_groups applies, because a source this
                # sweep may not fetch is a source it has no business opening a
                # browser at either -- the compliance sign-off included.
                MonitorTarget.is_enabled.is_(True),
                HotelSource.is_active.is_(True),
                Hotel.is_active.is_(True),
                Source.is_enabled.is_(True),
                Source.tos_reviewed_at.is_not(None),
                # Written by discovery, so re-deriving it is in kind. An engine
                # profile carries no note and is left alone.
                HotelSource.adapter_config["discovery_note"].is_not(None),
            )
            .order_by(HotelSource.id)
        ).all()

        seen: set[int] = set()
        for hotel_source, target in rows:
            if hotel_source.id in seen:
                continue

            # The decision itself is pure and lives beside the rules it
            # belongs to -- what makes a config worth offering to the current
            # scanner, and why this caller waits where a fetch does not.
            if not needs_rescan(
                hotel_source.adapter_config,
                now=now,
                cooldown_minutes=settings.auto_rediscovery_cooldown_minutes,
            ):
                continue

            stale += 1
            if requested >= limit:
                continue

            # The window this target is actually monitored on. A repair given
            # no window renders a URL with no dates in it, and a booking page
            # without dates shows either nothing or a different room list --
            # either way, not the page whose config is being repaired.
            stay = resolve_stay_window(
                strategy=target.date_strategy,
                today=today,
                fixed_check_in=target.fixed_check_in,
                fixed_check_out=target.fixed_check_out,
                lead_time_days=target.lead_time_days,
                length_of_stay_nights=target.length_of_stay_nights,
            )
            if stay is None:
                # A fixed window that has passed. dispatch_due_checks disables
                # these; nothing to repair against in the meantime.
                continue

            seen.add(hotel_source.id)
            requested += 1
            rediscover_source.apply_async(
                args=[hotel_source.id],
                kwargs={
                    "check_in": stay.check_in.isoformat(),
                    "check_out": stay.check_out.isoformat(),
                    "adults": target.adults,
                    "children": target.children,
                    "reason": f"scanner_generation_{DISCOVERY_VERSION}",
                },
                queue="browser",
            )

    if stale:
        log.info(
            "stale_configs_swept",
            stale=stale,
            requested=requested,
            generation=DISCOVERY_VERSION,
        )
    return {"stale": stale, "requested": requested}
