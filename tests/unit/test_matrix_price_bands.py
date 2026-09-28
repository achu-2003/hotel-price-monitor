"""The matrix laid out by price: one column per 500.

What this pins: which column a room lands in (a band holds its start up to
the next one's, so 1,500 is in the 1,500 column and 1,999 is too), that
only bands with a room in them become columns, and that a sold-out room's
last rate never lands in a price column as though it could be booked.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.dashboard.routes import _price_bands


def _cell(room, price, available=True):
    return {"room_name": room, "price": price, "is_available": available}


def _entry(name, *cells):
    return {"hotel": SimpleNamespace(name=name), "cells": list(cells)}


def test_columns_are_the_500_bands_rooms_fall_in_cheapest_first():
    rows = [_entry("A", _cell("r", 2600)), _entry("B", _cell("r", 1200), _cell("s", 1700))]
    assert _price_bands(rows) == [1000, 1500, 2500]


def test_a_band_nobody_sells_in_is_not_a_column():
    rows = [_entry("A", _cell("r", 1000), _cell("s", 3000))]
    assert _price_bands(rows) == [1000, 3000]


def test_a_band_holds_its_start_up_to_the_next_start():
    a = _entry("A", _cell("low", 1500), _cell("high", Decimal("1999.99")), _cell("next", 2000))
    _price_bands([a])
    assert [c["room_name"] for c in a["bands"][1500]] == ["low", "high"]
    assert [c["room_name"] for c in a["bands"][2000]] == ["next"]


def test_rooms_in_one_band_are_cheapest_first():
    a = _entry("A", _cell("dear", 1900), _cell("cheap", 1600))
    _price_bands([a])
    assert [c["room_name"] for c in a["bands"][1500]] == ["cheap", "dear"]


def test_a_sold_out_room_goes_to_its_own_column_not_a_price_band():
    a = _entry("A", _cell("open", 4000), _cell("full", 9000, available=False))
    assert _price_bands([a]) == [4000]
    assert [c["room_name"] for c in a["sold_out_cells"]] == ["full"]


def test_nothing_bookable_means_no_price_columns():
    a = _entry("A", _cell("full", 9000, available=False))
    b = _entry("B")
    assert _price_bands([a, b]) == []
    assert a["bands"] == {} and b["bands"] == {}
