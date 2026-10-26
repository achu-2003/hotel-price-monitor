"""Goibibo: every room, at the price the card shows, on the board it sells.

The fixture is Goibibo's own search-rooms answer for AGS Holiday Resorts,
28 Oct 2026, 1 room, 2 adults, trimmed to the fields the adapter reads.
On the page that night the Classic Room read "₹5,214 + ₹600 taxes & service
fees", with 7,500 struck through beside it.
"""
from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.adapters.base import FetchContext
from app.adapters.engines import detect
from app.adapters.goibibo import offers_from_search_rooms
from app.core.errors import SchemaDriftError
from app.services import meal_plan
from app.services.dates import StayWindow

PAYLOAD = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "goibibo" / "search-rooms-ags-2026-10-28.json")
    .read_text(encoding="utf-8")
)

URL = ("https://www.goibibo.com/hotels/hotel-details/?checkin=20261023&checkout=20261024"
       "&roomString=1-2-0&searchText=AGS%20Holiday%20Resorts&locusId=CTXYG&locusType=city"
       "&cityCode=CTXYG&cc=IN&_uCurrency=INR&vcid=CTXYG&giHotelId=1264690103492052130"
       "&mmtId=2017031713172679&sType=hotel")


def context(adults: int = 2) -> FetchContext:
    return FetchContext(
        hotel_source_id=1, hotel_name="AGS Holiday Resorts", url=URL, external_id=None,
        stay=StayWindow(date(2026, 10, 28), date(2026, 10, 29)), adults=adults,
    )


def by_room_and_board(offers):
    return {(o.raw_room_name, o.meal_plan): o for o in offers}


class TestThePriceIsTheOneOnTheCard:
    def test_the_default_coupon_not_the_base_fare_or_a_bank_card(self):
        """7,500 is struck through; 5,019 needs an ICICI card; 5,214 is shown."""
        classic = by_room_and_board(offers_from_search_rooms(PAYLOAD, context()))[
            ("Classic Room", meal_plan.ROOM_ONLY)]
        assert classic.price_exclusive == D("5214")
        assert classic.taxes_fees == D("600")
        assert classic.price_inclusive == D("5814")

    def test_every_room_and_board_is_read(self):
        offers = by_room_and_board(offers_from_search_rooms(PAYLOAD, context()))
        assert offers[("Deluxe Room", meal_plan.BREAKFAST)].price_exclusive == D("5774")
        assert offers[("Suite Room", meal_plan.HALF_BOARD)].price_exclusive == D("10569")
        assert offers[("Luxury villa", meal_plan.BREAKFAST)].price_exclusive == D("11017")
        assert len(offers) == 12

    def test_the_room_code_and_terms_travel_with_it(self):
        classic = offers_from_search_rooms(PAYLOAD, context())[0]
        # The id survives a rename; see ota-room-renames in services/ingest.py.
        assert classic.site_room_id == "2977"
        assert classic.refundable is True
        assert classic.rooms_left == 32


class TestWhatIsNotAPrice:
    def test_a_tariff_for_another_party_is_not_this_ones(self):
        assert offers_from_search_rooms(PAYLOAD, context(adults=3)) == []

    def test_a_default_key_with_no_entry_is_drift_not_a_sold_out(self):
        broken = copy.deepcopy(PAYLOAD)
        for room in broken["response"]["exactRooms"]:
            for plan in room["ratePlans"]:
                for tariff in plan["tariffs"]:
                    tariff["defaultPriceKey"] = "RENAMED"
        with pytest.raises(SchemaDriftError):
            offers_from_search_rooms(broken, context())

    def test_no_rooms_is_an_empty_list(self):
        """The adapter decides sold-out against the page text; this stays neutral."""
        assert offers_from_search_rooms({"response": {"exactRooms": []}}, context()) == []

    def test_a_payload_without_a_response_is_drift(self):
        with pytest.raises(SchemaDriftError):
            offers_from_search_rooms({"error": "nope"}, context())


class TestTheLinkFollowsTheNight:
    def test_dates_and_party_become_placeholders(self):
        d = detect(URL)
        assert d.profile.key == "goibibo" and d.profile.adapter_key == "goibibo"
        assert d.external_id == "1264690103492052130"
        assert d.is_complete
        assert "checkin={check_in:%Y%m%d}" in d.url_template
        assert "checkout={check_out:%Y%m%d}" in d.url_template
        assert "roomString=1-{adults}-{children}" in d.url_template
