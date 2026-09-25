"""Schema drift is reported only when a fresh page load shows it again.

On 24 Sep a Treebo page was read while its room list still showed a spinner,
and ASG's Booking.com page came back with no text at all. Both were filed as
a redesign -- a red row and a repair attempt each -- and both read perfectly
on the next check. The first sighting now earns a second look a minute later;
only a second sighting is recorded.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.errors import SchemaDriftError, TimeoutError_
from app.workers import tasks_fetch


class _Retry(Exception):
    def __init__(self, countdown):
        self.countdown = countdown


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def handled(monkeypatch):
    """_handle_failure with the database and Celery replaced by recorders."""
    seen = SimpleNamespace(errors=[], failures=[], repairs=[], runs=[])
    monkeypatch.setattr(tasks_fetch, "sync_session", lambda: _Session())
    monkeypatch.setattr(tasks_fetch.monitoring, "record_error",
                        lambda session, **kw: seen.errors.append(kw["error"]))
    monkeypatch.setattr(tasks_fetch.monitoring, "record_failure",
                        lambda session, ids, error, now: seen.failures.append(error))
    monkeypatch.setattr(tasks_fetch, "_update_check_run",
                        lambda session, run_id, **kw: seen.runs.append(kw))
    monkeypatch.setattr(tasks_fetch, "_request_repair",
                        lambda payload, stay, **kw: seen.repairs.append(kw["reason"]))

    def run(error, attempt):
        def retry(exc=None, countdown=None, **_):
            raise _Retry(countdown)
        task = SimpleNamespace(request=SimpleNamespace(retries=attempt), retry=retry)
        payload = {"hotel_id": 1, "source_id": 2, "hotel_source_id": 3}
        return tasks_fetch._handle_failure(
            task, error, payload, stay=None, target_ids=[9], check_run_id="run",
            started=datetime.now(UTC), triggered_by="scheduler",
            logger=SimpleNamespace(warning=lambda *a, **k: None),
        )

    seen.run = run
    return seen


def _drift():
    return SchemaDriftError("No elements matched room_card selector 'tr.x'")


def test_the_first_sighting_looks_again_instead_of_reporting(handled):
    with pytest.raises(_Retry) as retried:
        handled.run(_drift(), attempt=0)
    assert retried.value.countdown >= tasks_fetch._DRIFT_CONFIRM_SECONDS
    assert handled.errors == [] and handled.failures == [] and handled.repairs == []


def test_the_check_run_still_records_that_it_failed(handled):
    with pytest.raises(_Retry):
        handled.run(_drift(), attempt=0)
    (run,) = handled.runs
    assert "unconfirmed" in run["error_summary"]


def test_a_second_sighting_is_reported_and_repaired_as_before(handled):
    result = handled.run(_drift(), attempt=1)
    assert result["status"] == "failed"
    assert len(handled.errors) == 1 and len(handled.failures) == 1
    assert handled.repairs == ["schema_drift"]


def test_other_errors_are_handled_exactly_as_before(handled):
    """A timeout is recorded on its first attempt and retried on its own schedule."""
    with pytest.raises(_Retry) as retried:
        handled.run(TimeoutError_("slow"), attempt=0)
    assert len(handled.errors) == 1
    assert retried.value.countdown < tasks_fetch._DRIFT_CONFIRM_SECONDS
