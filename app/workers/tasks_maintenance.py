"""Housekeeping that keeps the system honest while nobody is watching.

Four jobs, each addressing a way this system fails quietly rather than loudly:

``ensure_partitions``   next month's partition must exist BEFORE the first of
                        the month, or every insert fails at midnight
``alert_on_silence``    a target that stopped succeeding without erroring
``prune_artifacts``     screenshots and HTML fill a disk otherwise
``retention_sweep``     drop observation partitions past the retention window
"""
from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from celery import shared_task
from sqlalchemy import text

from app.config import get_settings
from app.core.logging import get_logger
from app.db.session import sync_session
from app.services import monitoring

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
    now = datetime.now(UTC)
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
    return {"stale": len(stale)}


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
