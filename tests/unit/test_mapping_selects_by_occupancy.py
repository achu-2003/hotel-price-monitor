"""Choosing a price by what was asked for, rather than by position.

THE INCIDENT THIS FILE IS ABOUT
===============================
Sterling Yelagiri recorded thirty-nine readings across seven days without a
single price change, and it was not because the hotel held its rates. Measured
against the live endpoint on one night:

    room                          stored    real 2-adult rate
    Classic Room                   100.00           12,000
    Classic room with Balcony    8,200.00           12,300
    Mountain View Classic Room   8,500.00           12,600

The config read ``defaultPrice`` -- a top-level field on the room object whose
name matches every price hint there is, and which the engine leaves at a
placeholder. All three were wrong, one by two orders of magnitude, and all
three were CONSTANT. A field that does not move produces a series that never
changes, and nothing about that is visible from the outside: the numbers are
plausible, the fetches succeed, the hotel simply looks quiet.

Discovery could not have found the real rate. It lives at

    pricing[adultCount=2].priceForPax.0.priceBeforeTax

among seventy-two entries per room -- one per rate plan x occupancy x date --
and the path language had integer indices only. ``pricing.0`` is the
SINGLE-adult rate: a real number, wrong by thousands, and impossible to notice
once stored. So the selector had to exist before the profile could be written.

``{adults}`` rather than a literal, because a configuration that hard-codes the
occupancy it was discovered at files one occupancy's rate under another's the
first time someone watches a different party size.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.adapters.base import FetchContext
from app.adapters.engines import detect
from app.adapters.mapping import booking_conditions, dig, offer_from_mapping
from app.core.errors import SchemaDriftError
from app.services.dates import StayWindow

#: The shape of one Hotelzify room, trimmed to what the mapping reads.
ROOM = {
    "roomName": "Classic Room",
    "availableRooms": 4,
    "defaultPrice": "100.00",
    "pricing": [
        {"adultCount": 1, "priceForPax": [{"checkInDate": "2026-09-12", "priceBeforeTax": 7003}]},
        {"adultCount": 2, "priceForPax": [{"checkInDate": "2026-09-12", "priceBeforeTax": 12000}]},
        {"adultCount": 3, "priceForPax": [{"checkInDate": "2026-09-12", "priceBeforeTax": 15400}]},
    ],
}

PATH = "pricing[adultCount={adults}].priceForPax.0.priceBeforeTax"


def conditions(adults: int = 2) -> dict:
    return booking_conditions(
        FetchContext(
            hotel_source_id=27,
            hotel_name="Sterling Yelagiri",
            url="https://booking.sterlingholidays.com/rooms/5171/x/y/2/0",
            external_id="5171",
            stay=StayWindow(date(2026, 9, 12), date(2026, 9, 13)),
            adults=adults,
        )
    )


class TestTheSelector:
    def test_it_reads_the_rate_for_the_occupancy_asked_for(self):
        assert dig(ROOM, PATH, None, params=conditions(2)) == 12000

    def test_a_different_occupancy_reads_a_different_rate(self):
        """The bug this prevents is subtle: 7003 is a real number, on the real
        payload, wrong only because nobody asked for one adult."""
        assert dig(ROOM, PATH, None, params=conditions(1)) == 7003

    def test_the_first_entry_is_not_the_answer(self):
        """``pricing.0`` -- what the old path language could express -- is the
        single-adult rate, and would have been wrong by five thousand."""
        assert dig(ROOM, "pricing.0.priceForPax.0.priceBeforeTax", None) != 12000

    def test_a_literal_value_works_too(self):
        assert dig(ROOM, "pricing[adultCount=3].priceForPax.0.priceBeforeTax", None) == 15400

    def test_numbers_and_strings_compare_alike(self):
        """JSON carries 2 and a config carries "2"; a mapping that broke when
        someone quoted a number would be a trap rather than a language."""
        assert dig(ROOM, "pricing[adultCount=2].priceForPax.0.priceBeforeTax", None) == 12000

    def test_no_match_falls_to_the_default(self):
        assert dig(ROOM, "pricing[adultCount=9].priceForPax.0.priceBeforeTax", "none") == "none"

    def test_a_missing_key_falls_to_the_default(self):
        assert dig(ROOM, "nosuch[adultCount=2].x", "none") == "none"

    def test_a_condition_the_fetch_does_not_carry_is_a_config_error(self):
        """Not a missing price. Guessing would file one party size's rate
        under another's, silently."""
        with pytest.raises(SchemaDriftError) as caught:
            dig(ROOM, "pricing[adultCount={pets}].priceForPax.0.priceBeforeTax", None,
                params=conditions(2))

        assert "pets" in str(caught.value)


class TestTheWholeOfferThroughTheProfile:
    """End to end on the profile as shipped, so a change to either half that
    breaks the pair is caught here."""

    def _profile(self):
        found = detect("https://booking.sterlingholidays.com/rooms/5171/2026-09-12/2026-09-13/2/0")
        assert found is not None, "the hotelzify profile no longer matches its own URL"
        return found.profile

    def test_the_engine_is_recognised(self):
        assert self._profile().key == "hotelzify"

    def test_the_offer_carries_the_real_rate(self):
        offer = offer_from_mapping(
            ROOM, self._profile().adapter_config["fields"],
            default_currency="INR", params=conditions(2),
        )

        assert offer.price_exclusive == 12000

    def test_the_placeholder_is_not_what_gets_stored(self):
        offer = offer_from_mapping(
            ROOM, self._profile().adapter_config["fields"],
            default_currency="INR", params=conditions(2),
        )

        assert offer.price_exclusive != 100
        assert offer.price_inclusive is None  # priceBeforeTax is not the all-in

    def test_a_room_with_no_rooms_left_reads_as_unavailable(self):
        offer = offer_from_mapping(
            {**ROOM, "availableRooms": 0}, self._profile().adapter_config["fields"],
            default_currency="INR", params=conditions(2),
        )

        assert offer.is_available is False
