"""The repricer's log leads with the rates it actually changed.

Every 30 minutes the rule decides about every room, and nearly every decision
is "unchanged" or "held". The few that wrote a rate to RMS -- the ones an owner
opens the log to find -- were buried among them, with no way to ask for just
those, or for one day's.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.db.models import RepricingAction, User
from app.services import repricing_data

TZ = "Asia/Kolkata"


@pytest.fixture
def owner(session):
    user = User(username="repricer-log-owner", password_hash="x")
    session.add(user)
    session.flush()
    return user


def _action(session, owner, at: datetime, *, status="applied", mode="auto",
            room="Standard Double", was="9952", now="8855") -> RepricingAction:
    row = RepricingAction(
        owner_user_id=owner.id, room_name=room, plan="CP", check_in=at.date(),
        mode=mode, status=status, competitors=[],
        current_rms=Decimal(was), applied_rms=Decimal(now) if status == "applied" else None,
        created_at=at,
    )
    session.add(row)
    session.flush()
    return row


def _log(session, owner, **kw):
    return session.scalars(repricing_data.log_stmt(owner.id, tz=TZ, **kw)).all()


def test_changes_only_shows_the_writes_and_the_refused_writes(session, owner):
    """A write RMS refused is the change an owner most needs to see."""
    at = datetime(2026, 9, 25, 5, 0, tzinfo=UTC)
    applied = _action(session, owner, at)
    for status in ("unchanged", "held", "proposed", "read"):
        _action(session, owner, at, status=status)
    refused = _action(session, owner, at, status="failed")

    assert _log(session, owner, changes_only=True) == [refused, applied]


def test_all_decisions_is_everything_but_the_plain_readings(session, owner):
    at = datetime(2026, 9, 25, 5, 0, tzinfo=UTC)
    for status in ("applied", "unchanged", "held", "read"):
        _action(session, owner, at, status=status)

    assert sorted(a.status for a in _log(session, owner, changes_only=False)) == [
        "applied", "held", "unchanged"]


def test_a_day_is_the_local_day_not_the_utc_one(session, owner):
    """00:58 IST on the 25th is 19:28 UTC on the 24th, and belongs to the 25th."""
    early_25th = _action(session, owner, datetime(2026, 9, 24, 19, 28, tzinfo=UTC))
    late_24th = _action(session, owner, datetime(2026, 9, 24, 18, 0, tzinfo=UTC))
    _action(session, owner, datetime(2026, 9, 25, 18, 31, tzinfo=UTC))  # 00:01 on the 26th

    assert _log(session, owner, changes_only=True, day=date(2026, 9, 25)) == [early_25th]
    assert _log(session, owner, changes_only=True, day=date(2026, 9, 24)) == [late_24th]


def test_the_newest_change_comes_first(session, owner):
    older = _action(session, owner, datetime(2026, 9, 25, 1, 0, tzinfo=UTC))
    newer = _action(session, owner, datetime(2026, 9, 25, 3, 0, tzinfo=UTC))
    assert _log(session, owner, changes_only=True) == [newer, older]


def test_last_automatic_change_is_the_newest_applied_auto_row(session, owner):
    """A manual Apply after it, or a newer decision that wrote nothing, is not
    the automatic mode doing something."""
    auto = _action(session, owner, datetime(2026, 9, 25, 1, 0, tzinfo=UTC))
    _action(session, owner, datetime(2026, 9, 25, 2, 0, tzinfo=UTC), mode="manual")
    _action(session, owner, datetime(2026, 9, 25, 3, 0, tzinfo=UTC), status="unchanged")

    assert session.scalar(repricing_data.last_auto_change_stmt(owner.id)) == auto


def test_no_automatic_change_yet_is_none(session, owner):
    _action(session, owner, datetime(2026, 9, 25, 1, 0, tzinfo=UTC), mode="manual")
    assert session.scalar(repricing_data.last_auto_change_stmt(owner.id)) is None


def test_todays_count_is_automatic_writes_on_that_local_day(session, owner):
    day = date(2026, 9, 25)
    _action(session, owner, datetime(2026, 9, 24, 19, 28, tzinfo=UTC))   # 00:58 on the 25th
    _action(session, owner, datetime(2026, 9, 25, 6, 0, tzinfo=UTC))
    _action(session, owner, datetime(2026, 9, 25, 6, 0, tzinfo=UTC), mode="manual")
    _action(session, owner, datetime(2026, 9, 25, 6, 0, tzinfo=UTC), status="held")
    _action(session, owner, datetime(2026, 9, 24, 12, 0, tzinfo=UTC))    # the 24th

    assert session.scalar(repricing_data.auto_changes_on_stmt(owner.id, day, TZ)) == 2
