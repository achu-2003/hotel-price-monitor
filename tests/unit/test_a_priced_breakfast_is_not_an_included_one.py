"""Telling "breakfast ₹590" from "breakfast included".

Booking.com prints both in the same cell, in the same style, for two rows of
the same room:

    Good breakfast ₹ 590        room only, breakfast sold separately
    Good breakfast included     breakfast in the rate

On 24 Sep 2026 those two rows of Sterling's Classic room were 4,343 and
5,693. Matching on the word "breakfast" picks whichever comes first and is
wrong about half the time, by roughly 1,350 rupees -- thirteen times the gap
the repricing rule exists to hold.

Every string below is verbatim from a real page, captured from ASG's and
Sterling's Booking.com listings.
"""
from __future__ import annotations

import pytest

from app.services.meal_plan import BREAKFAST, HALF_BOARD, ROOM_ONLY, classify, wanted


class TestTheRealRowsFromBookingDotCom:
    """Captured from the live pages on 24 Sep 2026."""

    @pytest.mark.parametrize("text", [
        "Good breakfast ₹ 590 | Non-refundable | • | Pay online",
        "Breakfast ₹ 500 (optional) | Free cancellation before 23 September 2026 | • | Pay online",
    ])
    def test_a_breakfast_with_a_price_beside_it_is_room_only(self, text):
        assert classify(text) == ROOM_ONLY

    @pytest.mark.parametrize("text", [
        "Good breakfast included | Non-refundable | • | Pay online",
        "Continental breakfast included | Free cancellation before 23 September 2026 | • | Pay online",
    ])
    def test_an_included_breakfast_is_breakfast(self, text):
        assert classify(text) == BREAKFAST

    def test_breakfast_and_dinner_is_half_board(self):
        text = ("Breakfast & dinner included | Non-refundable | "
                "Flexibility to change your dates | • | Pay online")
        assert classify(text) == HALF_BOARD


class TestThePhrasingsThatWouldFoolAWordMatch:
    def test_the_meal_word_alone_decides_nothing(self):
        assert classify("Breakfast") is None

    @pytest.mark.parametrize("text", [
        "Breakfast INR 450",
        "Breakfast Rs. 450",
        "Breakfast 450",
        "Breakfast not included",
        "Breakfast excluded",
    ])
    def test_every_way_of_selling_it_separately_is_room_only(self, text):
        assert classify(text) == ROOM_ONLY

    @pytest.mark.parametrize("text", [
        "Free breakfast",
        "Includes breakfast",
        "Breakfast is included in the price",
        "Rate inclusive of breakfast",
    ])
    def test_every_way_of_giving_it_away_is_breakfast(self, text):
        assert classify(text) == BREAKFAST

    def test_something_else_being_included_is_not_a_meal(self):
        """"Free WiFi included" says nothing about board, and a rule that
        read it as room-only would have deleted its own doubt."""
        assert classify("Free WiFi included | Free cancellation") is None

    def test_a_dinner_only_rate_is_not_mistaken_for_breakfast(self):
        assert classify("Dinner included") == HALF_BOARD


class TestNothingIsGuessed:
    @pytest.mark.parametrize("text", [None, "", "   ", "Pay online", "Non-refundable"])
    def test_text_that_does_not_say_returns_nothing(self, text):
        assert classify(text) is None

    def test_an_unreadable_plan_never_matches_what_was_asked_for(self):
        """The caller wanted breakfast. "We could not tell" is not breakfast,
        and a rule that took it as one would price the room against whatever
        happened to be in that row."""
        assert wanted(None, BREAKFAST) is False
        assert wanted(ROOM_ONLY, BREAKFAST) is False
        assert wanted(BREAKFAST, BREAKFAST) is True
