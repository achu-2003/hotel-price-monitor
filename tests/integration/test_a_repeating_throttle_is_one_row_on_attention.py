"""A throttle that keeps happening is one row on Attention, not one per attempt.

On 13 Nov 2026 Goibibo refused ASG, Ananthyam and Sterling every fifteen
minutes from 08:30, and Attention showed eighteen rows saying the same thing.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.errors import BlockedError, ErrorClass, RateLimitedError
from app.db.models import MonitoringError
from app.services import monitoring


def _open_rows(session, target):
    return session.execute(
        select(MonitoringError).where(MonitoringError.monitor_target_id == target.id,
                                      MonitoringError.resolved_at.is_(None))
    ).scalars().all()


def _throttle(session, target, at, shot):
    return monitoring.record_error(
        session,
        error=RateLimitedError("Goibibo sent no room list", retry_after_seconds=900,
                               context={"screenshot_path": shot}),
        hotel_id=None, source_id=None, target_id=target.id, check_run_id=None, now=at,
    )


def test_repeats_update_one_row_with_a_count_and_the_latest_evidence(session, hotel_fixture):
    target = hotel_fixture["target"]
    first = datetime(2026, 11, 13, 3, 2, tzinfo=UTC)
    for i in range(6):
        _throttle(session, target, first + timedelta(minutes=15 * i), f"/data/artifacts/{i}.png")
    session.flush()

    rows = _open_rows(session, target)
    assert len(rows) == 1
    row = rows[0]
    assert row.retry_count == 5
    assert row.occurred_at == first + timedelta(minutes=75)
    assert row.screenshot_path == "/data/artifacts/5.png"
    assert datetime.fromisoformat(row.context["first_seen_at"]) == first


def test_the_next_success_still_clears_it_and_a_new_throttle_opens_a_new_row(session, hotel_fixture):
    target = hotel_fixture["target"]
    t0 = datetime.now(UTC) - timedelta(hours=1)
    _throttle(session, target, t0, "a.png")
    _throttle(session, target, t0 + timedelta(minutes=15), "b.png")
    session.flush()
    monitoring.record_success(session, [target.id])
    assert _open_rows(session, target) == []

    _throttle(session, target, datetime.now(UTC) + timedelta(minutes=1), "c.png")
    session.flush()
    rows = _open_rows(session, target)
    assert len(rows) == 1 and rows[0].retry_count == 0


def test_an_error_that_needs_a_person_is_still_one_row_each(session, hotel_fixture):
    target = hotel_fixture["target"]
    now = datetime.now(UTC)
    for i in range(2):
        monitoring.record_error(session, error=BlockedError("bot wall"), hotel_id=None,
                                source_id=None, target_id=target.id, check_run_id=None,
                                now=now + timedelta(minutes=i))
    session.flush()
    rows = [r for r in _open_rows(session, target) if r.error_class == ErrorClass.BLOCKED]
    assert len(rows) == 2
