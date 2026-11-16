"""A source we are deliberately spacing out is not "gone quiet" on Attention.

On 16 Nov 2026 Goibibo refused the production server for hours. Its three
hotels were each genuinely stale -- no successful check in three intervals --
and filled the "Gone quiet" list with an alarm nobody could act on, while the
adaptive pacing was already handling the refusal. "Gone quiet" is for a target
that stopped with NO explanation (a stopped scheduler); a paced source has one,
so it is left off that list.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.db.models.monitoring import MonitorTarget
from app.services.monitoring import gone_quiet

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def target(last_success_minutes_ago, interval=30, created_minutes_ago=600):
    """A stand-in carrying the real ``is_stale`` so staleness is not re-stated."""
    ns = SimpleNamespace(
        interval_minutes=interval,
        last_success_at=NOW - timedelta(minutes=last_success_minutes_ago),
        created_at=NOW - timedelta(minutes=created_minutes_ago),
    )
    ns.is_stale = lambda now: MonitorTarget.is_stale(ns, now)
    return ns


def test_a_stale_target_with_no_pacing_is_gone_quiet():
    # Seven hours quiet, nothing holding it back: the dangerous silent freeze.
    assert gone_quiet(target(last_success_minutes_ago=420), NOW, source_paced=False)


def test_a_stale_target_whose_source_is_paced_is_not():
    # Same silence, but the back-off explains it -- the throttle lane owns it.
    assert not gone_quiet(target(last_success_minutes_ago=420), NOW, source_paced=True)


def test_a_fresh_target_is_never_gone_quiet_paced_or_not():
    assert not gone_quiet(target(last_success_minutes_ago=5), NOW, source_paced=False)
    assert not gone_quiet(target(last_success_minutes_ago=5), NOW, source_paced=True)
