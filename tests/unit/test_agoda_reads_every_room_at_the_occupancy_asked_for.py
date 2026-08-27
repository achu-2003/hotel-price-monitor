"""Agoda: every room type, at the occupancy that was searched for.

THE INCIDENT
============
An Agoda listing for Ananthyam Resort raised

    1 of 2 offers shared an identity with another offer in the same fetch AT A
    DIFFERENT PRICE ... add a meal_plan or refundable selector so the two can
    be told apart.

The advice was wrong, and following it would have made things worse. The two
offers were not two rate plans. They were the SAME room at two occupancies,
which Agoda returns side by side for a single search::

    Family Room   Max 2 adults   13,500   isFit=true    <- the 2-adult rate
    Family Room   Max 4 adults   14,400   isFit=false

A meal_plan selector would have split them into two series and faithfully
recorded a 4-adult rate as though someone were watching it. What was actually
needed was to not read the second row at all: ``isFit`` is Agoda's own word
for "this row answers the search you made".

AND A SECOND, QUIETER BUG
=========================
Auto-discovery wrote ``roomGridData.masterRooms.0.rooms``. ``masterRooms`` is
one entry PER ROOM TYPE, so ``.0`` is the first room type and nothing else.
This property has four. Three were never read, and three rooms that are never
read look exactly like three rooms that do not exist -- no error, no empty
result, just a hotel that appears to sell one room.

AND A THIRD, WHICH NO SELECTOR CAN FIX
======================================
Occupancy was not the whole story. On a later night the same listing returned
the same room, at the SAME occupancy, twice -- because Agoda is a marketplace
and lists one row per supplier::

    Deluxe   supplierId 332    breakfast included   4,950
    Deluxe   supplierId 3038   room only            5,500

Room name, occupancy and board together still do not identify one of these,
and the cheaper row is the one WITH breakfast -- so "add a meal_plan selector"
fails here in the other direction too. The only honest fix is to state which
of several quotes for one room gets recorded, and record that decision:
``rooms_dedupe: cheapest``, the figure the listing leads with.

The fixtures are trimmed captures of the real endpoint on two nights, keeping
the fields these three bugs turn on.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.adapters.base import FetchContext, StayWindow
from app.adapters.engines import detect
from app.adapters.mapping import (
    booking_conditions,
    dedupe_offers,
    dig,
    filter_rooms,
    offer_from_mapping,
)

PAYLOAD = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "agoda_room_grid_2026-08-28.json")
    .read_text(encoding="utf-8")
)

#: The same listing on a night when Agoda had several SUPPLIERS per room --
#: same room, same occupancy, different price, nothing in the offer identity
#: to tell them apart. This is the shape no selector can fix.
MULTI_SUPPLIER = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "agoda_room_grid_multi_supplier.json")
    .read_text(encoding="utf-8")
)

LISTING = "https://www.agoda.com/ananthyam-resort-by-anukulas/hotel/yelagiri-in.html"

#: Read off the live grid for a 2-adult search: the "Max 2 adults" row of each
#: room type, and nothing else. Pre-tax, which is the figure Agoda leads with.
EXPECTED = {
    "Superior": 7650,
    "Villa with Garden View": 9900,
    "Family Room": 13500,
    "Suite": 15750,
}


def _profile():
    found = detect(LISTING)
    assert found is not None, "the agoda profile no longer matches its own URL"
    return found.profile


def _context(adults: int = 2) -> FetchContext:
    return FetchContext(
        hotel_source_id=1,
        hotel_name="Ananthyam Resort",
        url=LISTING,
        external_id=None,
        stay=StayWindow(check_in=date(2026, 8, 28), check_out=date(2026, 8, 29)),
        adults=adults,
        children=0,
        currency="INR",
    )


def _offers(payload=None, dedupe: bool = True):
    """The adapter's whole JSON path: locate, filter, map, dedupe."""
    config = _profile().adapter_config
    nodes = dig(payload if payload is not None else PAYLOAD, config["rooms_path"], None)
    nodes = filter_rooms(nodes, config["rooms_filter"])
    offers = [
        offer_from_mapping(n, config["fields"], default_currency="INR",
                           params=booking_conditions(_context()))
        for n in nodes
    ]
    return dedupe_offers(offers, config["rooms_dedupe"]) if dedupe else offers


class TestEveryRoomTypeIsRead:
    def test_the_grid_holds_more_rows_than_one_group(self):
        """Guards the fixture itself: a flattened capture would make the
        wildcard test below pass for the wrong reason."""
        assert len(PAYLOAD["roomGridData"]["masterRooms"]) == 4

    def test_the_old_path_saw_a_single_room_type(self):
        """What `.0` actually did, kept so the regression is legible."""
        assert len(dig(PAYLOAD, "roomGridData.masterRooms.0.rooms", None)) == 1

    def test_the_wildcard_path_reaches_every_room(self):
        assert len(dig(PAYLOAD, _profile().adapter_config["rooms_path"], None)) == 6

    def test_all_four_room_types_are_offered(self):
        assert {o.raw_room_name for o in _offers()} == set(EXPECTED)


class TestOnlyTheOccupancyAskedForIsRecorded:
    def test_no_two_offers_share_a_name(self):
        """The collision the alert was reporting."""
        names = [o.raw_room_name for o in _offers()]
        assert len(names) == len(set(names))

    def test_each_room_carries_its_two_adult_rate(self):
        got = {o.raw_room_name: int(o.price_exclusive) for o in _offers()}
        assert got == EXPECTED

    def test_the_four_adult_rate_is_not_recorded_anywhere(self):
        """13,500 is the answer; 14,400 is the answer to a different question."""
        prices = {int(o.price_exclusive) for o in _offers()}
        assert 14400 not in prices
        assert 16650 not in prices

    def test_the_dropped_rows_are_the_ones_agoda_marks_unfit(self):
        config = _profile().adapter_config
        nodes = dig(PAYLOAD, config["rooms_path"], None)
        dropped = [n for n in nodes if n not in filter_rooms(nodes, config["rooms_filter"])]
        assert dropped and all(n["isFit"] is False for n in dropped)


class TestTheFilterItself:
    def test_a_missing_spec_changes_nothing(self):
        rows = [{"a": 1}, {"a": 2}]
        assert filter_rooms(rows, None) == rows

    def test_a_quoted_boolean_still_matches(self):
        """JSON carries true and a config carries "true"."""
        rows = [{"isFit": True, "n": "keep"}, {"isFit": False, "n": "drop"}]
        assert filter_rooms(rows, {"field": "isFit", "equals": "true"}) == [rows[0]]

    def test_not_equals_inverts_it(self):
        rows = [{"isFit": True}, {"isFit": False}]
        assert filter_rooms(rows, {"field": "isFit", "not_equals": True}) == [rows[1]]

    def test_a_filter_that_matches_nothing_keeps_everything(self):
        """A field that was renamed must not be reported as a sell-out.

        Returning [] here would hand the pipeline an empty room list, which a
        source configured with sold_out_when_empty reads as "no rooms for
        sale" -- an outage invented by a stale config. Keeping the rows lets
        the collision alert fire and name the real problem instead.
        """
        rows = [{"isFit": True}, {"isFit": False}]
        assert filter_rooms(rows, {"field": "wasRenamed", "equals": True}) == rows


class TestTheProfileIsWiredForAnOta:
    def test_agoda_is_recognised(self):
        assert _profile().key == "agoda"

    def test_it_runs_on_the_ota_adapter_not_the_direct_site_one(self):
        """Consent is per-source: a hotel's own site being cleared says
        nothing about Agoda's terms, and every OTA fetch is logged as one."""
        assert _profile().adapter_key == "playwright_ota"

    @pytest.mark.parametrize("url", [
        LISTING,
        "https://www.agoda.com/en-in/sterling-yelagiri_2/hotel/yelagiri-in.html?checkIn=2026-09-15",
    ])
    def test_the_property_slug_is_lifted_from_the_url(self, url):
        assert detect(url).external_id


class TestOneRoomSoldBySeveralSuppliers:
    """isFit settles occupancy. It does not settle supply.

    Agoda is a marketplace: the same room at the same occupancy comes back once
    per supplier, and the cheaper of the pair is the one that INCLUDES
    breakfast -- so no meal_plan or refundable selector separates them either.
    """

    def test_the_payload_really_does_repeat_a_room(self):
        """Guards the fixture: without duplicates the rest proves nothing."""
        rooms = dig(MULTI_SUPPLIER, _profile().adapter_config["rooms_path"], None)
        fitting = [r["name"] for r in rooms if r["isFit"] is True]
        assert len(fitting) > len(set(fitting))

    def test_the_cheaper_supplier_is_the_one_with_breakfast(self):
        """Pins why 'just read the room-only rate' is not the answer here."""
        deluxe = [
            r for m in MULTI_SUPPLIER["roomGridData"]["masterRooms"]
            if m["name"] == "Deluxe" for r in m["rooms"]
        ]
        cheap = min(deluxe, key=lambda r: r["pricePopupViewModel"]["agodaPrice"])
        assert cheap["isBreakfastIncluded"] is True

    def test_every_room_is_reported_once(self):
        names = [o.raw_room_name for o in _offers(MULTI_SUPPLIER)]
        assert len(names) == len(set(names)) == 4

    def test_the_cheapest_supplier_wins(self):
        got = {o.raw_room_name: int(o.price_exclusive) for o in _offers(MULTI_SUPPLIER)}
        assert got == {
            "Deluxe": 4950,              # not the 5,500 second supplier
            "Superior": 6750,            # not 7,500
            "Villa with Garden View": 7650,
            "Family Room": 7650,         # not 11,700
        }

    def test_without_dedupe_the_pipeline_would_see_a_collision(self):
        """What the alert was reporting, kept so the regression is legible."""
        names = [o.raw_room_name for o in _offers(MULTI_SUPPLIER, dedupe=False)]
        assert len(names) - len(set(names)) == 3


class TestDedupeItself:
    def _offer(self, name, price, meal=None):
        from app.adapters.base import NormalizedOffer
        from decimal import Decimal
        return NormalizedOffer(raw_room_name=name, price_inclusive=Decimal(price),
                               meal_plan=meal, currency="INR")

    def test_off_by_default(self):
        offers = [self._offer("A", 100), self._offer("A", 200)]
        assert len(dedupe_offers(offers, None)) == 2

    def test_cheapest_and_dearest(self):
        offers = [self._offer("A", 200), self._offer("A", 100)]
        assert int(dedupe_offers(offers, "cheapest")[0].price_inclusive) == 100
        assert int(dedupe_offers(offers, "dearest")[0].price_inclusive) == 200

    def test_genuinely_different_products_both_survive(self):
        """A source that labels its boards properly keeps both series."""
        offers = [self._offer("A", 100, "Room Only"), self._offer("A", 150, "Breakfast")]
        assert len(dedupe_offers(offers, "cheapest")) == 2

    def test_input_order_is_preserved(self):
        offers = [self._offer("B", 100), self._offer("A", 100)]
        assert [o.raw_room_name for o in dedupe_offers(offers, "cheapest")] == ["B", "A"]

    def test_a_sold_out_row_never_displaces_a_priced_one(self):
        """Otherwise a sell-out is invented for a room that is on sale."""
        from app.adapters.base import NormalizedOffer
        sold_out = NormalizedOffer(raw_room_name="A", is_available=False, currency="INR")
        kept = dedupe_offers([self._offer("A", 100), sold_out], "cheapest")
        assert len(kept) == 1 and kept[0].price_inclusive is not None


class TestBothSidesOfTheTaxAreRecorded:
    """agodaPrice is PRE-TAX and used to be filed as price_inclusive.

    That read correctly while this deployment compares on "exclusive" -- the
    offer falls back to the other component when its own is empty -- and would
    have been silently wrong the day anyone compared on "inclusive". Not by a
    constant, either: Agoda applies a different rate per room.
    """

    def test_the_pre_tax_figure_lands_in_the_exclusive_slot(self):
        deluxe = [o for o in _offers(MULTI_SUPPLIER) if o.raw_room_name == "Deluxe"][0]
        assert int(deluxe.price_exclusive) == 4950

    def test_the_tax_inclusive_figure_lands_in_the_inclusive_slot(self):
        deluxe = [o for o in _offers(MULTI_SUPPLIER) if o.raw_room_name == "Deluxe"][0]
        assert float(deluxe.price_inclusive) == 5197.5

    def test_the_two_are_not_the_same_number(self):
        """The whole shape of the old bug: one value serving both slots."""
        for offer in _offers(MULTI_SUPPLIER):
            assert offer.price_exclusive != offer.price_inclusive

    def test_the_tax_rate_differs_between_rooms_of_one_hotel(self):
        """Why no single correction factor could have patched this."""
        rates = {
            o.raw_room_name: round(float(o.price_inclusive) / float(o.price_exclusive), 2)
            for o in _offers(MULTI_SUPPLIER)
        }
        assert rates["Deluxe"] == 1.05
        assert rates["Villa with Garden View"] == 1.18

    def test_either_basis_now_reads_its_own_number(self):
        deluxe = [o for o in _offers(MULTI_SUPPLIER) if o.raw_room_name == "Deluxe"][0]
        assert int(deluxe.price_on("exclusive")) == 4950
        assert float(deluxe.price_on("inclusive")) == 5197.5
