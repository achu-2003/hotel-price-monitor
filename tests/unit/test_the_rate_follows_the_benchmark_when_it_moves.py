"""The rate follows the benchmark as soon as a reading moves it.

Before this, a Sterling move read at :30 waited for the :56 tick to reach
RMS. Now the fetch of a benchmark hotel for tonight asks whether the figure
the rule would use has moved since the last run priced from it, and starts a
run if so -- without letting the redelivery guard swallow that run, and
without two browsers driving RMS for one owner at once.

Pure throughout: the database, Redis and Celery are all fakes.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.workers import tasks_repricing as task

TONIGHT = date(2026, 9, 24)
DELUXE, STANDARD, SUITE = 25, 27, 29


def _p(room_type_id, market, name=None):
    return SimpleNamespace(room_type_id=room_type_id, room_name=name or f"room {room_type_id}",
                           market=None if market is None else Decimal(market))


class TestWhichRoomsMoved:
    MAPPED = {DELUXE: object(), STANDARD: object()}

    def test_a_new_figure_is_a_move(self):
        moved = task.rooms_whose_benchmark_moved(
            [_p(DELUXE, "5379", "Deluxe")], self.MAPPED, {DELUXE: Decimal("5978.00")})
        assert moved == ["Deluxe"]

    def test_the_same_figure_is_not(self):
        """Numeric(12,2) comes back as 5379.00; that is the same figure."""
        assert task.rooms_whose_benchmark_moved(
            [_p(DELUXE, "5379")], self.MAPPED, {DELUXE: Decimal("5379.00")}) == []

    def test_a_room_nothing_priced_tonight_counts_as_moved(self):
        assert task.rooms_whose_benchmark_moved([_p(DELUXE, "5379", "Deluxe")], self.MAPPED, {}) == ["Deluxe"]

    def test_a_room_with_no_benchmark_figure_is_left_alone(self):
        """Not paired, or Sterling not on sale: there is nothing to follow."""
        assert task.rooms_whose_benchmark_moved([_p(DELUXE, None)], self.MAPPED, {}) == []

    def test_an_unmapped_room_is_left_alone(self):
        assert task.rooms_whose_benchmark_moved([_p(SUITE, "9000")], self.MAPPED, {}) == []


class _Session:
    def __init__(self, owners):
        self.owners = owners

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def scalars(self, _stmt):
        return iter(self.owners)


@pytest.fixture
def world(monkeypatch):
    """benchmark_moved over one owner, with the rule's answer and the last run's figure set per test."""
    queued: list = []
    w = SimpleNamespace(owners=[2], proposals=[_p(DELUXE, "5379", "Deluxe")],
                        last={DELUXE: Decimal("5978.00")}, queued=queued)
    monkeypatch.setattr(task, "local_today", lambda tz: TONIGHT)
    monkeypatch.setattr(task, "sync_session", lambda: _Session(w.owners))
    monkeypatch.setattr(task, "compute", lambda s, o, ci, co: (w.proposals, None, {DELUXE: object()}))
    monkeypatch.setattr(task, "_last_market", lambda s, o, night: w.last)
    monkeypatch.setattr(task.run_repricing, "apply_async",
                        lambda args, kwargs=None, queue=None: queued.append((args, kwargs, queue)))
    return w


class TestAReadingThatMovesTheFigureStartsARun:
    def test_a_moved_figure_queues_a_run_now(self, world):
        task.benchmark_moved(10, TONIGHT.isoformat())
        ((args, kwargs, queue),) = world.queued
        assert args == [2, "auto"]
        assert kwargs["trigger"] == "benchmark" and kwargs["trigger_id"]
        assert queue == "browser"

    def test_an_unchanged_figure_starts_nothing(self, world):
        world.last = {DELUXE: Decimal("5379.00")}
        task.benchmark_moved(10, TONIGHT.isoformat())
        assert world.queued == []

    def test_nobody_with_automatic_repricing_on_means_no_run(self, world):
        """The owner query filters on auto_enabled; nobody back, nothing run."""
        world.owners = []
        task.benchmark_moved(10, TONIGHT.isoformat())
        assert world.queued == []

    def test_a_reading_for_another_night_is_ignored(self, world):
        task.benchmark_moved(10, "2026-09-25")
        assert world.queued == []

    def test_each_request_gets_its_own_trigger_id(self, world):
        task.benchmark_moved(10, TONIGHT.isoformat())
        task.benchmark_moved(10, TONIGHT.isoformat())
        assert world.queued[0][1]["trigger_id"] != world.queued[1][1]["trigger_id"]


class _Reached(Exception):
    """Raised when a run gets past the redelivery guard."""


@pytest.fixture
def runner(monkeypatch):
    """run_repricing with the guard's inputs set per test and nothing past it."""
    calls = SimpleNamespace(last=None, lock=True, rerun=False, asked=0, released=0, queued=[])

    def reached():
        raise _Reached

    monkeypatch.setattr(task, "local_today", lambda tz: TONIGHT)
    monkeypatch.setattr(task.state, "read", lambda o, k: calls.last)
    monkeypatch.setattr(task.state, "write", lambda o, status, kind, **f: {"status": status, **f})
    monkeypatch.setattr(task, "sync_session", reached)
    monkeypatch.setattr(task, "_take_lock", lambda o: calls.lock)
    monkeypatch.setattr(task, "_release_lock", lambda o: setattr(calls, "released", calls.released + 1))
    monkeypatch.setattr(task, "_ask_rerun", lambda o: setattr(calls, "asked", calls.asked + 1))
    monkeypatch.setattr(task, "_take_rerun", lambda o: calls.rerun)
    monkeypatch.setattr(task.run_repricing, "apply_async",
                        lambda args, kwargs=None, queue=None: calls.queued.append((args, kwargs)))
    return calls


def _done_minutes_ago(minutes, **fields):
    from datetime import UTC, datetime, timedelta
    return {"status": "done", "check_in": TONIGHT.isoformat(), "scope": "all",
            "updated_at": (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(), **fields}


class TestTheRedeliveryGuardDoesNotSwallowATriggeredRun:
    def test_a_benchmark_run_five_minutes_after_a_tick_runs(self, runner):
        runner.last = _done_minutes_ago(5, trigger="tick")
        with pytest.raises(_Reached):
            task.run_repricing.run(2, "auto", trigger="benchmark", trigger_id="new")

    def test_the_same_trigger_id_again_is_a_redelivery(self, runner):
        runner.last = _done_minutes_ago(1, trigger="benchmark", trigger_id="same")
        result = task.run_repricing.run(2, "auto", trigger="benchmark", trigger_id="same")
        assert result["repeat"] is True

    def test_an_untagged_run_inside_the_window_is_still_a_redelivery(self, runner):
        """What the guard was for, unchanged."""
        runner.last = _done_minutes_ago(3)
        result = task.run_repricing.run(2, "auto")
        assert result["repeat"] is True


class TestNoSecondBrowserForOneOwner:
    def test_a_run_already_going_leaves_a_note_instead(self, runner):
        runner.last = {"status": "running", "updated_at": "2026-09-24T06:00:00+00:00"}
        result = task.run_repricing.run(2, "auto", trigger="benchmark", trigger_id="x")
        assert result["status"] == "deferred"
        assert runner.asked == 1

    def test_the_lock_held_by_another_worker_leaves_a_note_instead(self, runner):
        runner.lock = False
        result = task.run_repricing.run(2, "auto", trigger="benchmark", trigger_id="x")
        assert result["status"] == "deferred"
        assert runner.asked == 1

    def test_a_finished_run_that_was_asked_again_starts_one_more_pass(self, runner):
        runner.rerun = True
        with pytest.raises(_Reached):
            task.run_repricing.run(2, "auto", trigger="tick")
        ((args, kwargs),) = runner.queued
        assert args == [2, "auto"] and kwargs["trigger"] == "rerun"
        assert runner.released == 1

    def test_a_manual_run_takes_no_lock(self, runner):
        """The page already refuses a second run while one is going."""
        runner.lock = False
        with pytest.raises(_Reached):
            task.run_repricing.run(2, "manual")
        assert runner.asked == 0 and runner.released == 0
