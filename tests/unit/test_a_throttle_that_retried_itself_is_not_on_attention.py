"""Attention shows a "retries by itself" error only once it has failed to.

On 29 Oct Goibibo throttled about one load in three for ASG and Sterling,
each followed by a good reading inside the half hour, and every one sat on
Attention in red until then. A transient error is resolved by its target's
next success, so it is shown only when that success is overdue: two
intervals without one. What a person must act on is shown at once.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.monitoring import needs_a_person

NOW = datetime(2026, 10, 29, 9, 10, tzinfo=UTC)


def error(transient=True):
    return SimpleNamespace(is_transient=transient)


def target(last_success_minutes_ago=None, interval=30, created_minutes_ago=600):
    return SimpleNamespace(
        interval_minutes=interval,
        last_success_at=None if last_success_minutes_ago is None
        else NOW - timedelta(minutes=last_success_minutes_ago),
        created_at=NOW - timedelta(minutes=created_minutes_ago),
    )


def test_a_throttle_after_a_recent_success_waits():
    """ASG at 14:38: last good reading 14:22."""
    assert not needs_a_person(error(), target(last_success_minutes_ago=16), NOW)


def test_a_throttle_that_has_not_cleared_in_two_intervals_is_shown():
    """ASG's morning: nothing from Goibibo between 09:26 and 11:28."""
    assert needs_a_person(error(), target(last_success_minutes_ago=61), NOW)


def test_the_patience_follows_the_target_s_own_interval():
    assert not needs_a_person(error(), target(last_success_minutes_ago=9, interval=5), NOW)
    assert needs_a_person(error(), target(last_success_minutes_ago=11, interval=5), NOW)


def test_a_target_that_never_succeeded_is_measured_from_its_creation():
    assert not needs_a_person(error(), target(None, created_minutes_ago=20), NOW)
    assert needs_a_person(error(), target(None, created_minutes_ago=90), NOW)


def test_what_needs_a_person_is_shown_at_once():
    """A block, drift or config fault does not fix itself on the next check."""
    assert needs_a_person(error(transient=False), target(last_success_minutes_ago=1), NOW)


def test_an_error_with_no_target_is_shown():
    assert needs_a_person(error(), None, NOW)
