"""Two different facts wearing the same badge.

Ananthyam Resort's "Villa with Garden View" sat on the hotel page reading::

    Villa with Garden View    2A    sold out    04 Sep 11:22

while every other room on the same table said 15:22. The room had not sold.
Booking.com had stopped listing it, and once the absence sweep confirmed that,
the series was marked unavailable -- after which it is never looked at again,
because the sweep only considers available series. That is deliberate and it
is what stops the row oscillating between "sold out" and "available" every
time a page renders short.

The cost was the clock beside it. It stops, and then recedes: a day later it
reads 04 Sep against a page that checked this morning, which is what a broken
scraper looks like. Somebody chasing that finds nothing wrong, because nothing
is wrong.

So the two are told apart on the screen. The rule is deliberately timid: it
only ever relabels a row that is ALREADY showing as unavailable, so the worst
a misjudgement can do is call a sold-out room unlisted. It cannot invent a
room, and it cannot touch a room that is on sale.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.dashboard.routes import unlisted_offer_keys

NOW = datetime(2026, 9, 4, 15, 22, tzinfo=UTC)


def _series(key, *, available, checked_at):
    return SimpleNamespace(
        offer_key=key, is_available=available, last_checked_at=checked_at
    )


class TestARoomStillOnThePage:
    def test_a_room_on_sale_is_never_called_unlisted(self):
        # Even four hours behind. An available row lagging is a different
        # problem and this is not the place that diagnoses it.
        rows = [
            _series("a", available=True, checked_at=NOW),
            _series("b", available=True, checked_at=NOW - timedelta(hours=4)),
        ]
        assert unlisted_offer_keys(rows) == set()

    def test_a_sold_out_room_checked_with_the_others_stays_sold_out(self):
        rows = [
            _series("a", available=True, checked_at=NOW),
            _series("b", available=False, checked_at=NOW),
        ]
        assert unlisted_offer_keys(rows) == set()

    def test_minutes_of_difference_are_not_evidence(self):
        # Same night, different occupancies, separate page loads. Ordinary.
        rows = [
            _series("a", available=True, checked_at=NOW),
            _series("b", available=False, checked_at=NOW - timedelta(minutes=9)),
        ]
        assert unlisted_offer_keys(rows) == set()


class TestARoomThatHasGone:
    def test_it_is_named(self):
        rows = [
            _series("suite", available=True, checked_at=NOW),
            _series("villa", available=False, checked_at=NOW - timedelta(hours=4)),
        ]
        assert unlisted_offer_keys(rows) == {"villa"}

    def test_the_comparison_is_against_the_newest_row_not_the_clock(self):
        # A hotel whose whole table is a day old has not lost a room; it has
        # stopped being checked, which the Attention page reports separately.
        stale = NOW - timedelta(days=1)
        rows = [
            _series("suite", available=True, checked_at=stale),
            _series("villa", available=False, checked_at=stale),
        ]
        assert unlisted_offer_keys(rows) == set()

    def test_several_can_go_at_once(self):
        rows = [
            _series("suite", available=True, checked_at=NOW),
            _series("villa", available=False, checked_at=NOW - timedelta(hours=4)),
            _series("cottage", available=False, checked_at=NOW - timedelta(hours=4)),
        ]
        assert unlisted_offer_keys(rows) == {"villa", "cottage"}


class TestTheAwkwardInputs:
    def test_an_empty_table_is_not_an_error(self):
        assert unlisted_offer_keys([]) == set()

    def test_a_row_that_has_never_been_checked_is_ignored(self):
        rows = [
            _series("suite", available=True, checked_at=NOW),
            _series("new", available=False, checked_at=None),
        ]
        assert unlisted_offer_keys(rows) == set()

    def test_rows_with_no_timestamps_at_all_yield_nothing(self):
        rows = [_series("a", available=False, checked_at=None)]
        assert unlisted_offer_keys(rows) == set()
