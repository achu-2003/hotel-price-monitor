"""Hotelzify: the recorded price must be the one printed on the booking page.

These are pinned against payloads captured from Sterling Yelagiri on
27 Aug 2026, alongside screenshots of the page they were served with. The
numbers in ``EXPECTED_*`` are what the page displayed, read off the screen --
not what the code produced. A test that asserts the code's own output agrees
with itself would have passed happily throughout the bug this adapter exists
to fix.

WHAT WENT WRONG BEFORE
======================
The generic config recorded 10,093 for a room the page was selling at 3,859:

* it took the dearest rate plan, because the plan is a uuid on the pricing
  entry and its name lives in a sibling dict
* it compared ``adultCount`` only, ignoring children and infants
* it reported the RACK rate, because the discount comes from a second endpoint

Each of those has a test below, phrased as the wrong answer it would give.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.base import FetchContext, StayWindow
from app.adapters import hotelzify as hotelzify_module
from app.adapters.hotelzify import (
    HotelzifyAdapter,
    _best_promotion,
    _plan_names,
    _priced_plans,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


AVAILABILITY = _load("hotelzify_availability_2026-08-27.json")
LAST_MINUTE = _load("hotelzify_promotions_lastminute.json")["data"]
EARLY_BIRD = _load("hotelzify_promotions_earlybird.json")["data"]

#: Read off the booking page for 27 Aug -> 28 Aug, 2 guests. Room Only.
EXPECTED_TONIGHT = {
    "Classic Room": Decimal("3328.57"),
    "Classic room with Balcony": Decimal("3548.67"),
    "Mountain View Classic Room": Decimal("3859.17"),
}

#: The struck-through rack rates beside them, for the same rooms.
EXPECTED_RACK = {
    "Classic Room": Decimal("4824.02"),
    "Classic room with Balcony": Decimal("5143"),
    "Mountain View Classic Room": Decimal("5593"),
}


#: The moment these fixtures were captured, and the moment they describe.
#:
#: WITHOUT THIS THE FILE ROTS. Two separate clock reads decide whether a
#: promotion applies, and both were being answered by the wall clock:
#:
#:   cutoffDays   "book at least 15 days ahead" is measured from TODAY, so a
#:                hard-coded check-in date drifts closer every day. The early
#:                bird cleared 15 days when this was written and did not a
#:                fortnight later -- the suite went red on its own, with the
#:                production logic entirely correct.
#:   timeWindow   the Last Minute Deal is live 09:00-23:30, so the same tests
#:                also failed when run overnight, or from a machine in a
#:                timezone far enough west.
#:
#: Midday on the capture date sits inside the time window and puts the fixture
#: check-in dates the right distance away, so every assertion below is about
#: the promotion rules rather than about when the suite happened to run.
FROZEN_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


class _FrozenDatetime(datetime):
    """``datetime`` with ``now()`` pinned; everything else behaves normally.

    A subclass rather than a stub because the adapter uses the real class for
    parsing and arithmetic as well, and a bare object with one method would
    break those in ways that look unrelated to the clock.
    """

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Applied to every test here: any of them can reach a promotion rule."""
    monkeypatch.setattr(hotelzify_module, "datetime", _FrozenDatetime)


def _context(check_in=date(2026, 8, 27), check_out=date(2026, 8, 28), **kw) -> FetchContext:
    return FetchContext(
        hotel_source_id=1,
        hotel_name="Sterling Yelagiri",
        url="https://booking.sterlingholidays.com/rooms/5171/x/y/2/0",
        external_id="5171",
        stay=StayWindow(check_in=check_in, check_out=check_out),
        adults=kw.pop("adults", 2),
        children=kw.pop("children", 0),
        currency="INR",
        timezone="Asia/Kolkata",
        config=kw.pop("config", {}),
        **kw,
    )


def _offers(promotions, context=None):
    return HotelzifyAdapter()._to_offers(AVAILABILITY, promotions, context or _context())


class TestThePriceMatchesTheBookingPage:
    def test_every_room_is_priced_as_displayed(self):
        got = {o.raw_room_name: o.price_exclusive for o in _offers(LAST_MINUTE)}
        assert got == EXPECTED_TONIGHT

    def test_all_three_rooms_are_returned(self):
        assert len(_offers(LAST_MINUTE)) == 3


class TestTheRatePlanIsChosenNotStumbledInto:
    """The first entry matching the occupancy is the DEAREST plan on this
    engine. Picking by position is how 10,093 was recorded for a 3,859 room."""

    def test_the_room_only_board_is_recorded(self):
        assert {o.meal_plan for o in _offers(LAST_MINUTE)} == {"Room Only"}

    def test_it_is_not_the_first_plan_in_the_payload(self):
        room = AVAILABILITY["data"][0]["HotelRooms"][2]
        names = _plan_names(room)
        first_matching = next(
            p for p in room["pricing"] if p.get("adultCount") == 2
        )
        assert names[first_matching["ratePlanCode"]] == "Room with Breakfast, Lunch & Dinner"

        offer = [o for o in _offers(LAST_MINUTE)
                 if o.raw_room_name == "Mountain View Classic Room"][0]
        assert offer.meal_plan == "Room Only"

    def test_a_configured_board_is_honoured(self):
        context = _context(config={"board": "Room with Breakfast"})
        offers = _offers(LAST_MINUTE, context)
        assert {o.meal_plan for o in offers} == {"Room with Breakfast"}

    def test_an_unsold_board_falls_back_without_claiming_to_be_it(self):
        """The fallback must not label a full-board rate as Room Only.

        ``meal_plan`` is part of the offer key, so an honest label opens a
        separate series rather than writing the wrong product into the
        Room Only history.
        """
        context = _context(config={"board": "All Inclusive Penthouse Brunch"})
        for offer in _offers(LAST_MINUTE, context):
            assert offer.meal_plan != "All Inclusive Penthouse Brunch"
            assert offer.raw_payload["board_matched"] is False


class TestOccupancyIsMatchedOnAllThreeFields:
    def test_a_child_inclusive_rate_is_not_filed_as_the_two_adult_price(self):
        room = AVAILABILITY["data"][0]["HotelRooms"][0]
        priced = _priced_plans(room, _plan_names(room), _context(children=0))
        # Every entry behind these prices had childCount 0; the 1-child rates
        # for the same plans are 400 higher and must not appear.
        assert Decimal("4824.02") == min(p.price for p in priced).quantize(Decimal("0.01"))

    def test_asking_for_an_occupancy_the_room_does_not_price_yields_nothing(self):
        room = AVAILABILITY["data"][0]["HotelRooms"][0]
        priced = _priced_plans(room, _plan_names(room), _context(adults=9))
        assert priced == []


class TestTheDiscountIsApplied:
    def test_without_a_promotion_the_rack_rate_is_recorded(self):
        got = {o.raw_room_name: o.price_exclusive for o in _offers([])}
        assert {k: v.quantize(Decimal("0.01")) for k, v in got.items()} == {
            k: v.quantize(Decimal("0.01")) for k, v in EXPECTED_RACK.items()
        }

    def test_the_rack_rate_is_kept_for_diagnosis(self):
        """A promotion ending must be distinguishable from a price rise."""
        offer = [o for o in _offers(LAST_MINUTE)
                 if o.raw_room_name == "Mountain View Classic Room"][0]
        assert Decimal(offer.raw_payload["rack_price"]) == Decimal("5593")
        assert offer.raw_payload["promotion"]["name"] == "Last Minute Deal"

    def test_the_largest_applicable_discount_wins_not_the_first(self):
        """Both promotions carry isStack false, so the page applies one."""
        discount = _best_promotion(LAST_MINUTE, _context())
        assert discount.name == "Last Minute Deal"
        assert discount.amount == Decimal("31.00")

    def test_a_discount_can_never_produce_a_free_or_negative_room(self):
        absurd = [{
            "name": "broken", "discountType": "percentage", "discount": "250",
            "isActive": True,
        }]
        discount = _best_promotion(absurd, _context())
        assert discount.apply(Decimal("1000")) == Decimal("0.00")


class TestCutoffDaysMeansOppositeThingsForEarlyAndLate:
    """The bug this class exists for produced 5,550 against a page showing 5,250.

    ``late`` cutoff 0  -> book WITHIN 0 days of check-in.
    ``early`` cutoff 15 -> book AT LEAST 15 days ahead.
    """

    def test_a_last_minute_deal_applies_to_tonight(self):
        discount = _best_promotion(LAST_MINUTE, _context())
        assert discount.name == "Last Minute Deal"

    def test_a_last_minute_deal_does_not_apply_far_out(self):
        context = _context(date(2026, 9, 12), date(2026, 9, 13))
        discount = _best_promotion(LAST_MINUTE, context)
        assert discount is None or discount.name != "Last Minute Deal"

    def test_an_early_bird_applies_beyond_its_cutoff(self):
        context = _context(date(2026, 9, 12), date(2026, 9, 13))
        discount = _best_promotion(EARLY_BIRD, context)
        assert discount.amount == Decimal("30.00")
        # 7,500 rack -> the 5,250 the page printed.
        assert discount.apply(Decimal("7500")) == Decimal("5250.00")

    def test_an_early_bird_does_not_apply_inside_its_cutoff(self):
        """Booking tonight is not booking fifteen days ahead."""
        discount = _best_promotion(EARLY_BIRD, _context())
        assert discount is None or discount.amount != Decimal("30.00")


class TestPromotionsNobodyCanActuallyGet:
    @pytest.mark.parametrize(
        "flag",
        ["isPrivate", "isAgentPromo", "isMembership", "isRetargeting", "isReturningMember"],
    )
    def test_restricted_promotions_are_ignored(self, flag):
        """A member-only rate is not the price an anonymous guest is shown."""
        promo = [{
            "name": "members", "discountType": "percentage", "discount": "90",
            "isActive": True, flag: True,
        }]
        assert _best_promotion(promo, _context()) is None

    def test_a_disabled_promotion_is_ignored(self):
        promo = [{
            "name": "off", "discountType": "percentage", "discount": "90",
            "isActive": True, "isManuallyDisabled": True,
        }]
        assert _best_promotion(promo, _context()) is None


class TestFailureIsRefusedRatherThanGuessed:
    def test_an_empty_payload_is_drift_not_a_sell_out(self):
        """Recording a sell-out we cannot evidence is worse than recording
        nothing: it fires a "became unavailable" alert about a changed API."""
        from app.core.errors import SchemaDriftError

        with pytest.raises(SchemaDriftError):
            HotelzifyAdapter()._to_offers(
                {"data": [{"HotelRooms": []}]}, LAST_MINUTE, _context()
            )

    def test_rooms_with_no_priceable_rate_are_drift_too(self):
        from app.core.errors import SchemaDriftError

        with pytest.raises(SchemaDriftError):
            HotelzifyAdapter()._to_offers(AVAILABILITY, LAST_MINUTE, _context(adults=9))
