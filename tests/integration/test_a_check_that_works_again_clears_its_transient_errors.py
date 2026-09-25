"""A check that works again resolves the transient errors it has answered.

Booking.com served Sterling a 502 page at 13:33 on 24 Sep. The error said
"retries by itself", and it did -- the next check read the page -- but the
row stayed on Attention until somebody pressed Resolve. Now the successful
check closes it. Anything that needs a person stays open.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.errors import ErrorClass
from app.db.models import MonitoringError
from app.services import monitoring


def _error(session, target, *, transient: bool, error_class: ErrorClass, minutes_ago: int = 30):
    row = MonitoringError(
        monitor_target_id=target.id,
        occurred_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        error_class=error_class, is_transient=transient, message="test",
    )
    session.add(row)
    session.flush()
    return row


def test_a_transient_error_is_resolved_by_the_next_success(session, hotel_fixture):
    target = hotel_fixture["target"]
    error = _error(session, target, transient=True, error_class=ErrorClass.HTTP_STATUS)
    monitoring.record_success(session, [target.id])
    session.refresh(error)
    assert error.resolved_at is not None


def test_an_error_that_needs_a_person_stays_open(session, hotel_fixture):
    """A block says the site is refusing us; one good read does not undo that."""
    target = hotel_fixture["target"]
    error = _error(session, target, transient=False, error_class=ErrorClass.BLOCKED)
    monitoring.record_success(session, [target.id])
    session.refresh(error)
    assert error.resolved_at is None


def test_schema_drift_is_resolved_by_a_later_read_of_the_same_site(session, hotel_fixture):
    """The selectors just read rooms: whatever the drift was, it is not now."""
    target = hotel_fixture["target"]
    error = _error(session, target, transient=False, error_class=ErrorClass.PARSE_SCHEMA_DRIFT)
    monitoring.record_success(session, [target.id])
    session.refresh(error)
    assert error.resolved_at is not None


def test_drift_raised_by_the_success_itself_survives_it(session, hotel_fixture):
    """The collapsed-offers alert is recorded by a successful ingest, at the
    success's own timestamp, about that success."""
    target = hotel_fixture["target"]
    now = datetime.now(UTC)
    error = MonitoringError(monitor_target_id=target.id, occurred_at=now,
                            error_class=ErrorClass.PARSE_SCHEMA_DRIFT, is_transient=False,
                            message="offers collapsed")
    session.add(error)
    session.flush()
    monitoring.record_success(session, [target.id], now)
    session.refresh(error)
    assert error.resolved_at is None


def test_another_targets_error_is_left_alone(session, hotel_fixture):
    from app.db.models import DateStrategy, MonitorTarget

    target = hotel_fixture["target"]
    other = MonitorTarget(hotel_source_id=target.hotel_source_id, date_strategy=DateStrategy.FIXED,
                          fixed_check_in=target.fixed_check_in, fixed_check_out=target.fixed_check_out,
                          next_run_at=datetime.now(UTC), adults=3)
    session.add(other)
    session.flush()
    error = _error(session, other, transient=True, error_class=ErrorClass.NETWORK)
    monitoring.record_success(session, [target.id])
    session.refresh(error)
    assert error.resolved_at is None


def test_an_error_after_the_success_is_not_resolved_by_it(session, hotel_fixture):
    """A later failure is news the earlier success cannot have answered."""
    target = hotel_fixture["target"]
    error = _error(session, target, transient=True, error_class=ErrorClass.NETWORK, minutes_ago=-5)
    monitoring.record_success(session, [target.id])
    session.refresh(error)
    assert error.resolved_at is None


def test_the_collapsed_offers_alert_survives_later_successes(session, hotel_fixture):
    """Raised BY a success -- the page read fine and six rooms folded into one
    -- so the next success says nothing about whether it is fixed. It stays
    until a repair or a person resolves it, rather than closing and reopening
    every half hour."""
    target = hotel_fixture["target"]
    error = _error(session, target, transient=False, error_class=ErrorClass.PARSE_SCHEMA_DRIFT)
    error.context = {"names_seen": ["King Size Bed"]}
    session.flush()
    monitoring.record_success(session, [target.id])
    session.refresh(error)
    assert error.resolved_at is None
