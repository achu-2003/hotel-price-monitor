"""What the repricing rule makes of a market, and what its limits refuse.

Pure: rows go in, proposals come out. The rows are shaped like the matrix
query's ``(series, hotel, room_name)`` tuples, built by hand here.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.repricing import Rule, follow, guard, market_figure, propose, rms_bounds, to_rms

OURS = SimpleNamespace(id=9, name="AGS")
MGM = SimpleNamespace(id=4, name="MGM Winds")
ANANTHYAM = SimpleNamespace(id=1, name="Ananthyam")
THANGA = SimpleNamespace(id=12, name="Thanga Kottai")


def _series(room_type_id: int, excl: str, available: bool = True):
    return SimpleNamespace(
        room_type_id=room_type_id, is_available=available,
        last_price_exclusive=Decimal(excl), last_taxes_fees=Decimal("0"),
        last_price_inclusive=None, currency="INR",
    )


def _rows():
    return [
        (_series(25, "5610"), OURS, "Deluxe Double Room"),
        (_series(27, "6358"), OURS, "Standard Double Room"),
        (_series(101, "3400"), MGM, "Deluxe Room - King Bed"),
        (_series(102, "4250"), MGM, "Club Room - King and Sofa Bed - Sitout"),
        (_series(103, "5500"), ANANTHYAM, "Deluxe Double Room (2 Adults + 1 Child)"),
        (_series(104, "6500"), ANANTHYAM, "Superior Double Room"),
        (_series(105, "5224"), THANGA, "Deluxe Twin Room"),
        (_series(106, "5224"), THANGA, "Club Twin Room"),
        (_series(107, "6649"), THANGA, "Club Room"),
    ]


def test_the_market_is_the_median_of_each_hotels_entry_price():
    """Three hotels sell a deluxe: 3,400, 5,500 and 5,224. Ananthyam's dearer
    Superior Double is not the entry price and does not count."""
    (deluxe,) = [p for p in propose(_rows(), own_hotel_id=9, rule=Rule()) if p.room_type_id == 25]
    assert [c.price for c in deluxe.competitors] == [Decimal("3400"), Decimal("5224"), Decimal("5500")]
    assert deluxe.market == Decimal("5224")


def test_a_move_is_capped_at_the_step_and_says_so():
    """The market wants us at 5,220 from 5,610 (-7%); with a 5% step we go to 5,330."""
    rule = Rule(max_step_pct=Decimal("5"))
    (deluxe,) = [p for p in propose(_rows(), own_hotel_id=9, rule=rule) if p.room_type_id == 25]
    assert deluxe.target == Decimal("5330")
    assert deluxe.capped and "capped at -5%" in deluxe.capped
    assert deluxe.held is None


def test_a_number_outside_the_floor_is_held_not_written():
    target, held, _ = guard(Decimal("5610"), Decimal("3000"), Rule(max_step_pct=Decimal("100")))
    assert target == Decimal("3000")
    assert held and "below the floor" in held


def test_too_few_competitors_is_no_proposal():
    rule = Rule(min_competitors=5)
    (deluxe,) = [p for p in propose(_rows(), own_hotel_id=9, rule=rule) if p.room_type_id == 25]
    assert deluxe.target is None
    assert "only 3 competitor" in deluxe.held


def test_the_median_ignores_one_silly_price():
    assert market_figure([Decimal("5000"), Decimal("5200"), Decimal("5400"), Decimal("90000")]) == Decimal("5300")


def test_the_rms_rate_scales_by_the_channels_deal():
    """RMS says 7,500 where the site says 5,610. A site target of 6,000 is RMS 8,020."""
    assert to_rms(Decimal("6000"), Decimal("5610"), Decimal("7500"), round_to=10) == Decimal("8020")


def test_cp_and_map_keep_their_supplement_over_the_new_ep():
    assert follow(Decimal("8020"), Decimal("7500"), Decimal("8000"), round_to=10) == Decimal("8520")
    assert follow(Decimal("8020"), Decimal("7500"), Decimal("11000"), round_to=10) == Decimal("11520")


def test_the_rooms_rupee_floor_is_on_the_rms_number():
    """A floor of 7,500 is the grid's 7,500, not a site price."""
    assert rms_bounds(Decimal("6750"), floor=Decimal("7500"), ceiling=None) == "below this room's RMS floor of 7,500"
    assert rms_bounds(Decimal("7500"), floor=Decimal("7500"), ceiling=None) is None
    assert rms_bounds(Decimal("9100"), floor=None, ceiling=Decimal("9000")) == "above this room's RMS ceiling of 9,000"
