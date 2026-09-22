"""What the repricing rule makes of a market, and what its limits refuse.

Pure: rows go in, proposals come out. The rows are shaped like the matrix
query's ``(series, hotel, room_name)`` tuples, built by hand here.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.repricing import RunIn
from app.services.repricing import (
    Rule, follow, guard, market_figure, override, propose, rms_bounds, to_rms, usual_gaps,
)

OURS = SimpleNamespace(id=9, name="AGS")
MGM = SimpleNamespace(id=4, name="MGM Winds")
ANANTHYAM = SimpleNamespace(id=1, name="Ananthyam")
THANGA = SimpleNamespace(id=12, name="Thanga Kottai")


def _series(room_type_id: int, excl: str, available: bool = True, night: date = date(2026, 9, 18),
            source_id: int = 2):
    return SimpleNamespace(
        room_type_id=room_type_id, is_available=available, source_id=source_id,
        last_price_exclusive=Decimal(excl), last_taxes_fees=Decimal("0"),
        last_price_inclusive=None, currency="INR",
        check_in=night, check_out=night + timedelta(days=1),
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
    assert deluxe.wanted == Decimal("5220")  # the aim the cap moved it from
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


def test_a_typed_price_replaces_the_rules_and_skips_its_limits():
    """The rule caps a move at 5%; the owner typed 4,500 (-20%). That is written."""
    rule = Rule(max_step_pct=Decimal("5"))
    (deluxe,) = [p for p in propose(_rows(), own_hotel_id=9, rule=rule) if p.room_type_id == 25]
    mine = override(deluxe, Decimal("4500"))
    assert mine.target == Decimal("4500")
    assert mine.actionable
    assert mine.capped == "your price (the rule proposed 5,330)"
    # And it reaches RMS by the same ratio as the rule's would.
    assert to_rms(mine.target, mine.our_price, Decimal("7500"), round_to=10) == Decimal("6020")


def test_a_typed_price_lifts_a_hold_for_want_of_a_market():
    (deluxe,) = [p for p in propose(_rows(), own_hotel_id=9, rule=Rule(min_competitors=5)) if p.room_type_id == 25]
    assert not deluxe.actionable
    mine = override(deluxe, Decimal("6000"))
    assert mine.actionable
    assert mine.capped == "your price (the rule had no proposal)"


def test_a_typed_price_needs_a_price_of_ours_to_scale_from():
    rows = [(_series(25, "5610", available=False), OURS, "Deluxe Double Room")] + _rows()[2:]
    (deluxe,) = [p for p in propose(rows, own_hotel_id=9, rule=Rule()) if p.room_type_id == 25]
    mine = override(deluxe, Decimal("6000"))
    assert not mine.actionable
    assert "scale your price from" in mine.held


def test_the_apply_request_refuses_a_price_that_is_not_one():
    assert RunIn(mode="manual", overrides={"25": "6000"}).overrides == {25: Decimal("6000")}
    with pytest.raises(ValidationError):
        RunIn(mode="manual", overrides={25: 0})
    with pytest.raises(ValidationError):
        RunIn(mode="manual", overrides={25: -100})


# -- keep the usual gap, follow the market ----------------------------------

TONIGHT = date(2026, 9, 17)  # a Thursday: no weekend lift in play


def _deluxe_night(night: date, ours: str, market: tuple[str, str, str], sold_out: tuple = ()):
    """Our deluxe and three competitors' deluxe on one night."""
    rows = [(_series(25, ours, night=night), OURS, "Deluxe Double Room")]
    for hotel, price in zip((MGM, ANANTHYAM, THANGA), market):
        rows.append((_series(100 + hotel.id, price, available=hotel.name not in sold_out, night=night),
                     hotel, "Deluxe Room"))
    return rows


def _history(nights: int = 7, ours: str = "5200", market=("3600", "4000", "4400")):
    """We sold at 5,200 against a median of 4,000: 30% above it, every night."""
    rows = []
    for back in range(1, nights + 1):
        rows += _deluxe_night(TONIGHT - timedelta(days=back), ours, market)
    return rows


def _tonight(ours="5200", market=("3600", "4000", "4400"), **kw):
    rule = kw.pop("rule", Rule(max_step_pct=Decimal("20")))
    rows = _deluxe_night(TONIGHT, ours, market, sold_out=kw.pop("sold_out", ()))
    usual = usual_gaps(_history(), own_hotel_id=9)
    (deluxe,) = propose(rows, own_hotel_id=9, rule=rule, usual=usual, night=kw.pop("night", TONIGHT), **kw)
    return deluxe


def test_the_usual_gap_is_where_we_have_sat_against_the_median():
    assert usual_gaps(_history(), own_hotel_id=9) == {25: (Decimal("1.3000"), 7)}


def test_a_premium_the_room_always_had_is_not_cut():
    """30% above the market, as usual: the old rule cut this every run; this one leaves it."""
    deluxe = _tonight()
    assert deluxe.target == Decimal("5200")
    assert deluxe.change_pct == 0


def test_the_market_moving_down_moves_us_down_by_the_same_share():
    deluxe = _tonight(market=("3240", "3600", "3960"))  # median 4,000 -> 3,600, -10%
    assert deluxe.target == Decimal("4680")  # 5,200 - 10%


def test_the_market_moving_up_moves_us_up():
    deluxe = _tonight(market=("3960", "4400", "4840"))  # +10%
    assert deluxe.target == Decimal("5720")


def test_too_little_history_is_no_proposal():
    rows = _deluxe_night(TONIGHT, "5200", ("3600", "4000", "4400"))
    usual = usual_gaps(_history(nights=4), own_hotel_id=9)
    (deluxe,) = propose(rows, own_hotel_id=9, rule=Rule(), usual=usual, night=TONIGHT)
    assert deluxe.target is None
    assert "fewer than 5 nights of history" in deluxe.held


def test_sold_out_competitors_lift_the_price():
    """One of three sold out, a 12% lift at all sold out: +4%. The sold-out
    hotel stays in the median at its last price, so only the lift moves it."""
    deluxe = _tonight(market=("3600", "4000", "4400"), sold_out=("Ananthyam",),
                      rule=Rule(max_step_pct=Decimal("20"), sold_out_pct=Decimal("12")))
    assert deluxe.sold_out == ("Ananthyam",)
    assert deluxe.demand_pct == Decimal("4.0")
    assert "(+4%)" in deluxe.demand_note
    assert deluxe.target == Decimal("5410")  # 5,200 x 1.04, to the nearest 10
    assert "1 of 3 competitors sold out" in deluxe.demand_note


def test_the_weekend_lift_is_only_on_friday_and_saturday_nights():
    rule = Rule(max_step_pct=Decimal("20"), weekend_pct=Decimal("5"))
    assert _tonight(rule=rule).target == Decimal("5200")  # Thursday
    assert _tonight(rule=rule, night=date(2026, 9, 18)).target == Decimal("5460")  # Friday


def test_a_room_moves_once_a_night():
    """Written once tonight: the rule holds it, so automatic mode cannot walk it down."""
    deluxe = _tonight(market=("3240", "3600", "3960"), moved_today={25})
    assert deluxe.target == Decimal("4680")
    assert not deluxe.actionable
    assert "already moved once tonight" in deluxe.held
    # A price the owner types still goes.
    assert override(deluxe, Decimal("4900")).actionable


def test_the_dearest_hotel_selling_out_does_not_make_the_market_look_cheap():
    """Thanga Kottai at 4,400 sells out. Dropped, the median of the rest
    would fall to 3,800 and the rule would CUT on a busy night; kept at its
    last price, the median stays 4,000 and the sell-out lifts instead."""
    deluxe = _tonight(sold_out=("Thanga Kottai",))
    assert deluxe.market == Decimal("4000")
    assert [c.hotel for c in deluxe.competitors if c.sold_out] == ["Thanga Kottai"]
    assert deluxe.target > Decimal("5200")
