"""The Changes page: which date it filters on, and what "Alert" claims.

The delivery column is the interesting one. ``price_changes.notified`` is set
by the dispatcher even when NO recipient is assigned to the hotel -- on
purpose, so the change stops reappearing in every sweep -- so a column driven
by that flag reported "yes" for every row on an installation where nobody had
ever been told anything. These pin the distinction.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.dashboard.routes import _delivery_state, _parse_date


class _Change:
    def __init__(self, change_id: int, notified: bool):
        self.id = change_id
        self.notified = notified


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """Returns the notification rows it was given, whatever is asked."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = 0

    async def execute(self, _statement):
        self.queries += 1
        return _Result(self.rows)


class TestDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-08-19", date(2026, 8, 19)),
            ("  2026-08-19  ", date(2026, 8, 19)),
            ("", None),
            (None, None),
        ],
    )
    def test_iso_dates_are_read(self, raw, expected):
        assert _parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["rubbish", "19-08-2026", "2026-13-01", "yesterday"])
    def test_unparseable_input_is_ignored_rather_than_raised_at_someone(self, raw):
        """A typed URL should not answer with a 422."""
        assert _parse_date(raw) is None


class TestWhatTheAlertColumnClaims:
    @pytest.mark.asyncio
    async def test_processed_with_nobody_assigned_does_not_claim_a_delivery(self):
        """The exact state of an installation with no recipients.

        `notified` is true because the dispatcher finished with it, not
        because anyone heard.
        """
        session = _Session()
        state = await _delivery_state(session, [_Change(1, notified=True)])
        assert state[1][0] == "no one to tell"

    @pytest.mark.asyncio
    async def test_not_yet_dispatched_reads_as_pending(self):
        session = _Session()
        state = await _delivery_state(session, [_Change(2, notified=False)])
        assert state[2][0] == "pending"

    @pytest.mark.asyncio
    async def test_a_sent_notification_is_reported_as_sent(self):
        session = _Session([("sent", [7])])
        state = await _delivery_state(session, [_Change(7, notified=True)])
        assert state[7][0] == "sent"

    @pytest.mark.asyncio
    async def test_a_failure_is_not_hidden_behind_the_notified_flag(self):
        """The flag is true either way; the difference matters to a person."""
        session = _Session([("failed", [8])])
        state = await _delivery_state(session, [_Change(8, notified=True)])
        assert state[8][0] == "failed"

    @pytest.mark.asyncio
    async def test_one_digest_covering_several_changes_marks_them_all(self):
        """Batching is why the column is an array in the first place."""
        session = _Session([("sent", [10, 11, 12])])
        changes = [_Change(i, notified=True) for i in (10, 11, 12)]
        state = await _delivery_state(session, changes)
        assert [state[i][0] for i in (10, 11, 12)] == ["sent", "sent", "sent"]

    @pytest.mark.asyncio
    async def test_the_best_outcome_wins_when_a_change_was_retried(self):
        """A change that failed to one recipient and reached another was sent."""
        session = _Session([("failed", [13]), ("sent", [13])])
        state = await _delivery_state(session, [_Change(13, notified=True)])
        assert state[13][0] == "sent"

    @pytest.mark.asyncio
    async def test_no_changes_asks_the_database_nothing(self):
        session = _Session()
        assert await _delivery_state(session, []) == {}
        assert session.queries == 0
