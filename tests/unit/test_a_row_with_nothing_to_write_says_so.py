"""A row whose rate is already in RMS does not say "will apply".

30 Oct, automatic on: Goibibo's Deluxe Double read CP 11,684 -> 11,684 under
a green "will apply", and Booking.com's Standard Double the same at 11,904.
The first had been written by the last run and Goibibo had not caught up;
the second cannot move at all, because the 8,837 the rule wants sits in the
GST slab's gap and the closest price a guest can be shown is today's 7,875.
Every run found the cell already holding the rate and wrote nothing, while
the row read as though the automatic switch were off.

Pure: three rates in, one word out.
"""
from __future__ import annotations

from decimal import Decimal

from app.services import repricing as rule


class TestNothingToWrite:
    def test_our_own_write_waiting_for_the_site(self):
        """The cell moved, the site has not: base is the rate before it."""
        assert rule.nothing_to_write(Decimal("11684"), Decimal("11463"), Decimal("11684"),
                                     round_to=10) == "written"

    def test_a_rate_that_cannot_move(self):
        """No write of ours behind it: base is the cell itself."""
        assert rule.nothing_to_write(Decimal("11904"), Decimal("11904"), Decimal("11904"),
                                     round_to=10) == "stuck"

    def test_rounding_noise_is_still_nothing(self):
        assert rule.nothing_to_write(Decimal("11904"), Decimal("11904"), Decimal("11909"),
                                     round_to=10) == "stuck"

    def test_a_real_move_is_still_a_move(self):
        """Agoda's Deluxe Double: 5,700 -> 5,986 is a write, and says so."""
        assert rule.nothing_to_write(Decimal("5700"), Decimal("6941"), Decimal("5986"),
                                     round_to=10) is None

    def test_an_unread_grid_says_nothing(self):
        assert rule.nothing_to_write(None, None, None, round_to=10) is None
        assert rule.nothing_to_write(Decimal("5700"), Decimal("5700"), None, round_to=10) is None


class TestTheGstGap:
    def test_tonights_standard_double_target_is_in_it(self):
        assert rule.in_gst_gap(Decimal("8837"))

    def test_both_edges_are_prices_a_guest_can_see(self):
        assert not rule.in_gst_gap(Decimal("7875"))
        assert not rule.in_gst_gap(Decimal("8852"))

    def test_the_gap_is_why_the_rate_stays(self):
        """The sum itself: 8,837 converts to the RMS rate already there."""
        assert rule.to_rms(Decimal("8837"), Decimal("7875"), Decimal("11904"),
                           round_to=10, exact=True, with_tax=True) == Decimal("11904")
