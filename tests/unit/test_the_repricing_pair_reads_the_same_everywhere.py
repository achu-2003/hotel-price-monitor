"""Your property and the benchmark show the repricing board's price on every grid.

On 25 Sep Sterling's Mountain View Classic Room read 8,982 on /repricing -- its
breakfast rate, with tax, the figure the rule prices against -- and 5,638 on
the matrix, which showed its room-only entry price. One hotel, one night, two
numbers, and no way to tell from either screen which one the rule was using.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services import rate_gap
from app.services.price_display import entry_rows

OWN, STERLING, OTHER = 9, 10, 4


def _row(hotel_id, room, board, price, *, available=True, room_type_id=None):
    series = SimpleNamespace(
        offer_key=f"{hotel_id}-{room}-{board}", room_type_id=room_type_id or hash((hotel_id, room)),
        meal_plan=board, is_available=available, currency="INR",
        current_price=Decimal(price), last_price_inclusive=Decimal(price),
        last_price_exclusive=None, last_taxes_fees=None, last_changed_at=None,
        last_checked_at=None,
    )
    hotel = SimpleNamespace(id=hotel_id, name={OWN: "ASG", STERLING: "Sterling", OTHER: "MGM"}[hotel_id],
                            is_own_property=hotel_id == OWN)
    return series, hotel, room


def _rows():
    return [
        _row(STERLING, "Mountain View Classic Room", "Room Only", "5638"),
        _row(STERLING, "Mountain View Classic Room", "Breakfast", "8982"),
        _row(OWN, "Standard Double Room", "Room Only", "7000"),
        _row(OWN, "Standard Double Room", "Breakfast", "8882"),
        _row(OTHER, "Classic Room", "Room Only", "4000"),
        _row(OTHER, "Classic Room", "Breakfast", "6000"),
    ]


def _prices(rows, boards):
    return {(h.name, room): s.current_price for s, h, room in entry_rows(rows, True, boards)}


BOARDS = {OWN: "Breakfast", STERLING: "Breakfast"}


def test_the_benchmark_and_your_property_show_the_breakfast_rate():
    prices = _prices(_rows(), BOARDS)
    assert prices[("Sterling", "Mountain View Classic Room")] == Decimal("8982")
    assert prices[("ASG", "Standard Double Room")] == Decimal("8882")


def test_every_other_hotel_keeps_its_entry_price():
    assert _prices(_rows(), BOARDS)[("MGM", "Classic Room")] == Decimal("4000")


def test_with_nothing_pinned_every_hotel_shows_its_entry_price():
    prices = _prices(_rows(), {})
    assert prices[("Sterling", "Mountain View Classic Room")] == Decimal("5638")
    assert prices[("ASG", "Standard Double Room")] == Decimal("7000")


def test_a_room_not_sold_on_the_board_still_has_a_price():
    rows = [_row(STERLING, "Classic Room", "Room Only", "4901")]
    assert _prices(rows, BOARDS)[("Sterling", "Classic Room")] == Decimal("4901")


def test_a_bookable_room_beats_a_sold_out_breakfast_rate():
    rows = [
        _row(STERLING, "Classic Room", "Room Only", "4901"),
        _row(STERLING, "Classic Room", "Breakfast", "7482", available=False),
    ]
    assert _prices(rows, BOARDS)[("Sterling", "Classic Room")] == Decimal("4901")


def test_the_comparison_grid_follows_the_same_rule():
    grid = rate_gap.build(_rows(), baseline_hotel_id=OWN, show_with_tax=True, boards=BOARDS)
    text = repr(grid)
    assert "8982" in text and "8882" in text
    assert "5638" not in text
