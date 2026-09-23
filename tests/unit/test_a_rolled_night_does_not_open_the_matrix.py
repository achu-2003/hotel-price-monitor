"""The comparison screen opens on the night the hotels were compared on.

THE INCIDENT THIS FILE IS ABOUT
===============================
On the morning of 4 Sep the price matrix showed one hotel. Ten had been
checked an hour earlier and all ten had prices, so the obvious readings were
all wrong: nothing had failed, nothing was unowned, no filter was set.

The page had defaulted to 5 Sep. Sterling was sold out for the night of the
4th, so the fetcher did what it is meant to do -- rolled forward and priced
the 5th as well (``_with_rollover`` in app/workers/tasks_fetch.py) -- and that
made 5 Sep the newest ``check_in`` in ``price_series``. The default was
``MAX(check_in)``, so a night belonging to the single hotel that happened to
be full became the night the comparison screen opened on, and the comparison
itself was one date-picker click away with nothing on the page pointing to it.

The rolled reading is correct and worth keeping. It just is not a default: it
is the answer to "what does the hotel that is full tonight want for tomorrow",
which is a question about one property. So the default is now the most recent
night that was actually asked about -- a night some check run covers -- and a
rolled night is reachable by typing its date, like any other.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.dashboard.routes import _default_night
from app.services.dates import local_today, next_weekend

USER = SimpleNamespace(id=7)

#: What the ten hotels were checked for.
MONITORED = SimpleNamespace(check_in=date(2026, 9, 4), check_out=date(2026, 9, 5))
#: What one sold-out hotel rolled forward onto.
ROLLED = SimpleNamespace(check_in=date(2026, 9, 5), check_out=date(2026, 9, 6))


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    """Answers each execute() with the next prepared row, and keeps the SQL."""

    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows.pop(0) if self.rows else None)


#: What tonight looks like when it has been priced.
TONIGHT = SimpleNamespace(check_in=local_today(), check_out=local_today() + timedelta(days=1))


class TestTonightWinsWhileItHasPrices:
    """The same failure arrived again by a road the check-run rule allows.

    On the server, ASG carried a SECOND Booking.com target watching a window
    weeks out. That night was genuinely asked about, so it passed the "was
    this monitored" test and, being the newest, became the default -- and the
    matrix opened on 30 Sep showing one property while ten hotels priced for
    tonight sat behind the date picker. Exactly the 4 Sep incident, with a
    legitimately monitored night in place of a rolled one.

    Tonight is also the night Repricing computes for itself, so this is what
    stops the two screens describing different nights.
    """

    @pytest.mark.asyncio
    async def test_tonight_is_the_default(self):
        session = _Session(TONIGHT)
        check_in, check_out, note = await _default_night(session, USER, 2)
        assert (check_in, check_out) == (TONIGHT.check_in, TONIGHT.check_out)
        assert note == "Showing tonight."

    @pytest.mark.asyncio
    async def test_it_costs_one_query_and_asks_nothing_about_check_runs(self):
        """The common case is the cheap one: no correlated EXISTS at all."""
        session = _Session(TONIGHT)
        await _default_night(session, USER, 2)
        assert len(session.statements) == 1
        assert "check_runs" not in str(session.statements[0])

    @pytest.mark.asyncio
    async def test_a_stay_longer_than_one_night_keeps_its_own_check_out(self):
        """Not tonight + 1: a two-night target files under its own window."""
        two = SimpleNamespace(check_in=local_today(), check_out=local_today() + timedelta(days=2))
        session = _Session(two)
        check_in, check_out, _ = await _default_night(session, USER, 2)
        assert (check_in, check_out) == (two.check_in, two.check_out)

    @pytest.mark.asyncio
    async def test_an_empty_tonight_falls_through_to_the_old_rule(self):
        """Before the first fetch of the day there is nothing to show."""
        session = _Session(None, MONITORED)
        check_in, check_out, note = await _default_night(session, USER, 2)
        assert (check_in, check_out) == (date(2026, 9, 4), date(2026, 9, 5))
        assert note == "Showing the most recent night monitored."


class TestWhichNightOpens:
    @pytest.mark.asyncio
    async def test_the_monitored_night_is_the_default(self):
        session = _Session(None, MONITORED)
        check_in, check_out, note = await _default_night(session, USER, 2)
        assert (check_in, check_out) == (date(2026, 9, 4), date(2026, 9, 5))
        assert note == "Showing the most recent night monitored."

    @pytest.mark.asyncio
    async def test_it_asks_for_a_night_a_check_run_covers(self):
        """The exclusion is in the SQL, not in a filter applied afterwards.

        A rolled night is not marked as one anywhere -- it is an ordinary row
        under its own dates -- so the only thing separating it from a night
        somebody asked about is whether a check run covers it.
        """
        session = _Session(None, MONITORED)
        await _default_night(session, USER, 2)
        sql = str(session.statements[1])
        assert "check_runs" in sql
        assert "EXISTS" in sql.upper()

    @pytest.mark.asyncio
    async def test_a_night_somebody_typed_in_by_hand_counts_as_asked_about(self):
        """Manual entry files a check run with no monitor target.

        Nothing scheduled it -- an operator did -- so joining through the
        target would drop it, and a hotel with no booking engine, priced only
        by hand, would have every one of its nights treated like a
        roll-forward: real prices that can never be the default.
        """
        session = _Session(None, MONITORED)
        await _default_night(session, USER, 2)
        sql = str(session.statements[1])
        assert "LEFT OUTER JOIN" in sql
        assert "check_runs.monitor_target_id IS NULL" in sql

    @pytest.mark.asyncio
    async def test_a_night_that_was_asked_about_costs_one_query_more(self):
        """Two, now that tonight is asked about first -- and only two.

        The point of the original assertion survives: the fallbacks below are
        not run speculatively, they are run when the one above them came back
        empty.
        """
        session = _Session(None, MONITORED)
        await _default_night(session, USER, 2)
        assert len(session.statements) == 2


class TestWhenNothingWasAskedAbout:
    """History older than the check-run table, or restored without it."""

    @pytest.mark.asyncio
    async def test_the_newest_collected_night_is_still_better_than_none(self):
        session = _Session(None, None, ROLLED)
        check_in, check_out, note = await _default_night(session, USER, 2)
        assert (check_in, check_out) == (date(2026, 9, 5), date(2026, 9, 6))
        assert note == "Showing the most recent night collected."

    @pytest.mark.asyncio
    async def test_the_fallback_does_not_ask_about_check_runs(self):
        session = _Session(None, None, ROLLED)
        await _default_night(session, USER, 2)
        assert "check_runs" not in str(session.statements[2])


class TestWhenNothingHasEverBeenCollected:
    @pytest.mark.asyncio
    async def test_the_coming_weekend_is_the_first_guess(self):
        session = _Session(None, None, None)
        check_in, check_out, note = await _default_night(session, USER, 2)
        weekend = next_weekend(local_today())
        assert (check_in, check_out) == (weekend.check_in, weekend.check_out)
        assert note == "Defaults to the coming weekend."
