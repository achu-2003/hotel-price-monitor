"""A room sold three ways is one room, and the grids must draw it once.

``price_series`` holds an OFFER, not a room. Booking.com sells the Deluxe
Double room-only, with breakfast, and with breakfast and dinner; a hotel
tracked on its own engine as well as an OTA has each of those twice again.
Handed straight to a grid that draws a cell per row, one room became this:

    Deluxe Double Room  ₹5,355 | Deluxe Double Room  ₹5,807 | Deluxe Double Room  ₹8,560

which reads as a property with three Deluxe Doubles rather than one sold
three ways, and pushes every competitor row off the screen underneath it.

The grids compare properties on their ENTRY price -- the figure a guest is
quoted first -- so that is the offer each room brings, and it is the same
figure the repricing rule prices against.

Pure: rows in, rows out.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.price_display import entry_rows

AGS = SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS")
STERLING = SimpleNamespace(id=10, name="Sterling")


def _offer(room_type_id, price, *, available=True):
    return SimpleNamespace(
        offer_key=f"k{room_type_id}-{price}", room_type_id=room_type_id,
        current_price=Decimal(price), last_price_exclusive=None,
        last_taxes_fees=None, last_price_inclusive=None,
        currency="INR", is_available=available,
    )


#: Tonight as the site actually served it: one Deluxe on three boards, one
#: Standard on one, and Sterling's Classic on two boards AND two sites.
ROWS = [
    (_offer(25, "4766"), AGS, "Deluxe Double Room"),
    (_offer(25, "5807"), AGS, "Deluxe Double Room"),
    (_offer(25, "8560"), AGS, "Deluxe Double Room"),
    (_offer(27, "5048"), AGS, "Standard Double Room"),
    (_offer(110, "5316"), STERLING, "Classic Room"),
    (_offer(110, "4040"), STERLING, "Classic Room"),
    (_offer(110, "3390"), STERLING, "Classic Room"),
]


class TestOneRoomOneRow:
    def test_a_room_sold_three_ways_appears_once(self):
        rooms = [name for _, _, name in entry_rows(ROWS, False)]
        assert rooms.count("Deluxe Double Room") == 1

    def test_and_so_does_one_tracked_on_two_sites(self):
        rooms = [name for _, _, name in entry_rows(ROWS, False)]
        assert rooms.count("Classic Room") == 1

    def test_seven_offers_are_three_rooms(self):
        assert len(entry_rows(ROWS, False)) == 3

    def test_a_room_with_one_offer_is_untouched(self):
        rooms = [name for _, _, name in entry_rows(ROWS, False)]
        assert rooms.count("Standard Double Room") == 1


class TestWhichOfferSurvives:
    def test_the_cheapest_one_is_the_entry_price(self):
        kept = {name: s.current_price for s, _, name in entry_rows(ROWS, False)}
        assert kept["Deluxe Double Room"] == Decimal("4766")
        assert kept["Classic Room"] == Decimal("3390")

    def test_a_room_on_sale_beats_a_cheaper_one_that_is_not(self):
        """A sold-out rate can be the lowest number on the card and is not a
        price anybody can pay. Availability decides before money does."""
        rows = [
            (_offer(25, "3000", available=False), AGS, "Deluxe Double Room"),
            (_offer(25, "5807"), AGS, "Deluxe Double Room"),
        ]
        (series, _, _), = entry_rows(rows, False)
        assert series.current_price == Decimal("5807")

    def test_a_room_with_nothing_on_sale_still_gets_a_cell(self):
        """Dropping it would shrink the grid on exactly the nights a full
        hotel is the thing worth seeing."""
        rows = [
            (_offer(25, "8560", available=False), AGS, "Deluxe Double Room"),
            (_offer(25, "5807", available=False), AGS, "Deluxe Double Room"),
        ]
        (series, _, _), = entry_rows(rows, False)
        assert series.current_price == Decimal("5807")


class TestTheGridStaysReadable:
    def test_rooms_keep_the_order_they_arrived_in(self):
        """Callers sort by hotel and the hotel's own room order. A grid that
        reshuffled on every fetch would be unreadable."""
        assert [name for _, _, name in entry_rows(ROWS, False)] == [
            "Deluxe Double Room", "Standard Double Room", "Classic Room"]

    def test_two_hotels_selling_the_same_room_name_stay_separate(self):
        rows = [
            (_offer(25, "5000"), AGS, "Classic Room"),
            (_offer(110, "4000"), STERLING, "Classic Room"),
        ]
        assert len(entry_rows(rows, False)) == 2

    def test_a_caller_with_no_room_ids_is_grouped_by_name(self):
        """The grids draw names, so two cells reading the same name are a
        repetition to whoever is looking, ids or no ids."""
        bare = SimpleNamespace(offer_key="a", current_price=Decimal("900"),
                               last_price_exclusive=None, last_taxes_fees=None,
                               last_price_inclusive=None, currency="INR",
                               is_available=True)
        bare2 = SimpleNamespace(offer_key="b", current_price=Decimal("700"),
                                last_price_exclusive=None, last_taxes_fees=None,
                                last_price_inclusive=None, currency="INR",
                                is_available=True)
        rows = [(bare, AGS, "Deluxe Room"), (bare2, AGS, "Deluxe  room")]
        (series, _, _), = entry_rows(rows, False)
        assert series.current_price == Decimal("700")
