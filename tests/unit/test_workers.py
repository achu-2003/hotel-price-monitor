"""Worker-layer logic that needs no broker, no database, and no browser.

The task bodies themselves need Postgres and are covered by the integration
suite. What is tested here is everything a task decides BEFORE it touches
either: serialisation across the broker, retry policy, and the circuit breaker.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters import registry
from app.core.errors import (
    AdapterConfigError,
    BlockedError,
    ErrorClass,
    NetworkError,
    RateLimitedError,
    RobotsDisallowedError,
    SchemaDriftError,
    TimeoutError_,
    classify,
)
from app.db.models.enums import CircuitState, DateStrategy
from app.services.dates import StayWindow
from app.services.monitoring import CIRCUIT_COOLDOWN, DueGroup, _circuit_allows
from app.workers.tasks_fetch import _lock_name, _retry_delay, _stay_from_payload, group_to_payload


class _FakeTarget:
    """Enough of a MonitorTarget for the circuit breaker, without a database."""

    def __init__(self, state=CircuitState.CLOSED, opened_at=None):
        self.id = 1
        self.circuit_state = state
        self.circuit_opened_at = opened_at


class TestRegistry:
    def test_queues_are_declared_without_importing_the_adapter(self):
        # The beat process routes tasks without loading Playwright.
        assert registry.queue_for("playwright_direct_site") == "browser"
        assert registry.queue_for("http_json") == "http"

    def test_manual_entry_is_never_scheduled(self):
        assert registry.queue_for("manual_entry") == "manual"

    def test_unknown_key_raises_a_permanent_error(self):
        # AdapterConfigError is non-transient: a typo is not fixed by retrying.
        with pytest.raises(AdapterConfigError):
            registry.queue_for("does_not_exist")
        with pytest.raises(AdapterConfigError):
            registry.get_adapter("does_not_exist")

    def test_every_declared_adapter_has_a_queue(self):
        for key in registry.available_keys():
            assert registry.queue_for(key)


class TestGroupSerialisation:
    def _group(self):
        return DueGroup(
            hotel_id=1,
            hotel_name="ABC Resort",
            hotel_source_id=7,
            source_id=3,
            adapter_key="http_json",
            queue="http",
            url="https://example.test/book",
            external_id=None,
            currency="INR",
            adapter_config={"endpoint": "https://example.test/api"},
            stay=StayWindow(datetime(2026, 8, 20).date(), datetime(2026, 8, 21).date()),
            adults=2,
            children=0,
            rooms=1,
            meal_plan_filter=None,
            target_ids=[11, 12],
            rate_limit_per_min=6,
        )

    def test_payload_is_json_safe(self):
        """Dates must cross the broker as strings, never as date objects."""
        import json

        payload = group_to_payload(self._group())
        assert json.loads(json.dumps(payload)) == payload

    def test_round_trips_the_stay_window(self):
        payload = group_to_payload(self._group())
        stay = _stay_from_payload(payload)
        assert stay.check_in == datetime(2026, 8, 20).date()
        assert stay.nights == 1

    def test_lock_covers_everything_that_changes_the_request(self):
        # Two targets differing only in their alert thresholds resolve to the
        # same page load and must share one lock; different occupancy must not.
        payload = group_to_payload(self._group())
        assert _lock_name(payload) == "fetch:7:2026-08-20:2026-08-21:2:0"

        other = dict(payload, adults=3)
        assert _lock_name(other) != _lock_name(payload)

    def test_group_lock_key_matches_the_task_lock_name(self):
        # These are computed in two places; if they ever disagree, the lock
        # silently stops protecting anything.
        group = self._group()
        assert group.lock_key == _lock_name(group_to_payload(group))


class TestRetryPolicy:
    def test_backoff_grows_and_is_jittered(self):
        first = _retry_delay(NetworkError("x"), 0)
        second = _retry_delay(NetworkError("x"), 1)
        assert 30 <= first < 40
        assert 120 <= second < 155
        # Jitter is not cosmetic: without it, thirty hotels failing on one
        # network blip all retry in the same second and recreate it.
        assert len({round(_retry_delay(NetworkError("x"), 0), 4) for _ in range(20)}) > 1

    def test_honours_retry_after_on_429(self):
        delay = _retry_delay(RateLimitedError("slow down", retry_after_seconds=120), 0)
        assert delay >= 120

    def test_attempt_beyond_the_table_uses_the_last_step(self):
        assert _retry_delay(NetworkError("x"), 99) >= 480


class TestErrorClassification:
    def test_refusals_are_never_transient(self):
        # The two errors that must never be retried: both are a site saying no.
        assert BlockedError("bot wall").is_transient is False
        assert RobotsDisallowedError("nope").is_transient is False

    def test_schema_drift_is_permanent(self):
        # A redesign needs a human, not a retry — and no price is ever written.
        drift = SchemaDriftError("selector gone")
        assert drift.is_transient is False
        assert drift.error_class == ErrorClass.PARSE_SCHEMA_DRIFT

    def test_network_and_timeout_are_retried(self):
        assert NetworkError("reset").max_retries == 3
        assert TimeoutError_("slow").max_retries == 2

    def test_classify_passes_our_own_errors_through(self):
        original = BlockedError("captcha")
        assert classify(original) is original

    def test_classify_maps_third_party_exceptions(self):
        assert classify(TimeoutError("timed out")).error_class == ErrorClass.TIMEOUT
        assert classify(ConnectionResetError("reset")).error_class == ErrorClass.NETWORK


class TestCircuitBreaker:
    def test_closed_circuit_runs(self):
        assert _circuit_allows(_FakeTarget(), datetime.now(UTC)) is True

    def test_open_circuit_is_paused_during_the_cooldown(self):
        now = datetime.now(UTC)
        target = _FakeTarget(CircuitState.OPEN, now - timedelta(minutes=10))
        assert _circuit_allows(target, now) is False
        assert target.circuit_state == CircuitState.OPEN

    def test_cooldown_expiry_allows_one_probe(self):
        now = datetime.now(UTC)
        target = _FakeTarget(CircuitState.OPEN, now - CIRCUIT_COOLDOWN - timedelta(minutes=1))
        assert _circuit_allows(target, now) is True
        assert target.circuit_state == CircuitState.HALF_OPEN

    def test_half_open_does_not_send_a_second_probe(self):
        # Waiting for the first probe's verdict is the entire point; a second
        # would tell us nothing and double the load on a struggling site.
        target = _FakeTarget(CircuitState.HALF_OPEN)
        assert _circuit_allows(target, datetime.now(UTC)) is False


class TestBeatSchedule:
    def test_dispatch_runs_every_minute_and_expires(self):
        from app.workers.celery_app import celery_app

        entry = celery_app.conf.beat_schedule["dispatch-due-checks"]
        assert entry["schedule"] == 60.0
        # Expiry matters: after a beat restart, replaying a backlog of sweeps
        # would enqueue the same checks several times over.
        assert entry["options"]["expires"] < 60

    def test_all_tasks_are_registered(self):
        from app.workers.celery_app import celery_app

        for name in (
            "monitor.dispatch_due_checks",
            "fetch.prices",
            "fetch.record_manual_offers",
            "notify.dispatch_changes",
            "notify.send",
            "notify.release_quiet_hours",
            "maintenance.ensure_partitions",
            "maintenance.alert_on_silence",
        ):
            assert name in celery_app.tasks

    def test_serialisation_is_json_only(self):
        """Pickle would let anything with write access to Redis run code."""
        from app.workers.celery_app import celery_app

        assert celery_app.conf.accept_content == ["json"]
        assert celery_app.conf.task_serializer == "json"


class TestPartitionHelpers:
    def test_month_arithmetic_does_not_drift(self):
        from app.workers.tasks_maintenance import _add_months

        from datetime import date

        # timedelta(days=30) would drift and eventually produce a boundary
        # that is not the first of a month, which Postgres rejects.
        assert _add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
        assert _add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
        assert _add_months(date(2026, 8, 1), 12) == date(2027, 8, 1)

    def test_partition_name_parsing(self):
        from datetime import date

        from app.workers.tasks_maintenance import _month_from_name

        assert _month_from_name("price_observations_2026_08") == date(2026, 8, 1)
        assert _month_from_name("price_observations") is None
        assert _month_from_name("something_else_here") is None


def test_date_strategy_enum_values_are_stable():
    """These strings are stored in Postgres enums; renaming needs a migration."""
    assert DateStrategy.FIXED.value == "fixed"
    assert DateStrategy.ROLLING.value == "rolling"
