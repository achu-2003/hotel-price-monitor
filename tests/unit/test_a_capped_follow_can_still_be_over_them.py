"""A capped move can land on the WRONG SIDE of the hotel it follows.

Tonight's Standard Double: ours 9,913, Sterling 4,636. The rule wants 4,536
and the 50% step allows 4,960. The move is worth making -- five thousand
rupees in the right direction -- but 4,960 is 324 ABOVE the hotel the rule
exists to sit a hundred below, and the row said "will apply" in green with
the cap noted in grey beside it.

Both halves of that are true and the row showed only the flattering one. So
the proposal can now say how far short it is, and the page says it where the
verdict goes rather than in the small print.

Pure: proposals in, one number out.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services import repricing as rule

OURS = SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS")
STERLING = SimpleNamespace(id=10, name="Sterling")
OUR_ROOM = 27
STERLING_ONLY = rule.Benchmark(hotel_id=10, hotel="Sterling",
                               undercut=Decimal("100"), with_tax=False)


def _series(room_type_id, price, night):
    return SimpleNamespace(
        room_type_id=room_type_id, offer_key=f"k{room_type_id}-{price}",
        current_price=Decimal(price), last_price_exclusive=None, last_taxes_fees=None,
        last_price_inclusive=None, currency="INR", is_available=True,
        check_in=night, last_changed_at=None, last_checked_at=None,
        source_id=2, meal_plan=None,
    )


def _proposal(step_pct, *, ours="9913", theirs="4636"):
    night = date(2026, 9, 22)
    rows = [
        (_series(OUR_ROOM, ours, night), OURS, "Standard Double Room"),
        (_series(110, theirs, night), STERLING, "Classic Room"),
    ]
    (p,) = [x for x in rule.propose(
        rows, own_hotel_id=9,
        rule=rule.Rule(max_step_pct=Decimal(step_pct), floor_pct=Decimal("90"),
                       ceiling_pct=Decimal("500"), round_to=10),
        benchmark=STERLING_ONLY, night=night) if x.room_type_id == OUR_ROOM]
    return p


class TestWhenTheCapLeavesUsOverThem:
    def test_the_proposal_knows_it_did_not_reach_the_gap(self):
        p = _proposal("50")
        assert p.wanted == Decimal("4536")
        assert p.target > p.wanted
        assert p.short_by == p.target - Decimal("4536")

    def test_it_is_still_a_move_worth_making(self):
        """Not held: 4,960 is five thousand rupees nearer than 9,913."""
        p = _proposal("50")
        assert p.held is None
        assert p.target < Decimal("9913")

    def test_the_cap_is_still_named_as_the_reason(self):
        assert "capped at -50%" in _proposal("50").capped


class TestWhenTheGapIsKept:
    def test_an_uncapped_follow_is_short_by_nothing(self):
        p = _proposal("90")
        assert p.target == p.wanted == Decimal("4536")
        assert p.short_by is None

    def test_a_capped_rise_leaves_us_further_under_not_short(self):
        """THE CAP ONLY STRANDS US ON A WAY DOWN.

        A capped fall always stops above the target, which is the case
        above. A capped RISE stops below it: they moved to 10,000, the rule
        wants 9,900 and the step allows 4,500, so we sit five thousand
        further under them than asked. That is not falling short of the gap
        -- it is keeping it and then some -- and must not be flagged, or
        every slow climb would wear a warning.
        """
        p = _proposal("50", ours="3000", theirs="10000")
        assert p.capped and "capped at +50%" in p.capped
        assert p.target < p.wanted
        assert p.short_by is None


class TestItOnlyMeansSomethingForAFollow:
    def test_the_median_rule_has_no_fixed_gap_to_fall_short_of(self):
        night = date(2026, 9, 22)
        rows = [
            (_series(OUR_ROOM, "9913", night), OURS, "Standard Double Room"),
            (_series(110, "4636", night), STERLING, "Classic Room"),
            (_series(111, "4700", night), SimpleNamespace(id=4, name="MGM"), "Classic Room"),
        ]
        (p,) = [x for x in rule.propose(
            rows, own_hotel_id=9, rule=rule.Rule(max_step_pct=Decimal("50"), round_to=10),
            usual={OUR_ROOM: (Decimal("1.45"), 7)}, night=night)
            if x.room_type_id == OUR_ROOM]
        assert p.benchmark is None
        assert p.short_by is None
