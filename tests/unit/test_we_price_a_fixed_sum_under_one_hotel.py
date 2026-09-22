"""The owner prices against one competitor, a fixed sum under them.

Their rule, in their words: "we have set the price hundred rupees less".
Sterling moves 1,000 to 1,300 and we set 1,200; Sterling drops to 1,200 and
we set 1,100. It is a FOLLOW, and the things that make the median rule good
are the things that would break it:

    a median absorbs one odd     with one competitor there is nothing to
    reading                      absorb it with, so an unbookable or stale
                                 price must be refused outright rather
                                 than averaged away

    a median does not care       Sterling is tracked on Booking.com AND on
    which site quoted it         its own engine, and the two differ by
                                 hundreds. Whichever is cheaper is not an
                                 answer -- it prices our Booking.com room
                                 against their direct-booking discount

    a lift for a busy night      makes the gap something other than the
                                 hundred rupees the owner set

So the benchmark path refuses more than the median path does, and every
refusal here is a case where the median's own instinct is the wrong one.

The limits are the exception: step cap, floor, ceiling and one-move-a-night
still apply, because those are not part of the pricing idea. They are the
guard rail around a number that arrived from somebody else's website.

Pure throughout: rows in, proposals and prompts out. No database, no network.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services import repricing as rule
from app.services.repricing_advisor import SYSTEM, SYSTEM_BENCHMARK, Advice, Ask, advise

OURS = SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS")
STERLING = SimpleNamespace(id=10, name="Sterling")
MGM = SimpleNamespace(id=4, name="MGM Winds")

#: Our hotel and Sterling are both on Booking.com; Sterling is ALSO on its own
#: booking engine, which quotes less for the same room on the same night.
BOOKING_COM, STERLING_DIRECT = 2, 4

# The real shape of the night this was asked for: our Standard Double is a
# classic room, Sterling sells classic rooms, and our Deluxe Double has no
# Sterling counterpart at all.
OUR_CLASSIC, OUR_DELUXE = 27, 25

#: Most tests here are about the rule's shape, so they compare on the rate
#: alone and the fixtures carry no tax. The basis has its own class below.
STERLING_ONLY = rule.Benchmark(hotel_id=10, hotel="Sterling", undercut=Decimal("100"),
                               with_tax=False)

#: Limits wide open, for the tests about the rule's SHAPE. The limits have
#: their own tests below; leaving them at their defaults here would mean
#: every arithmetic assertion was really an assertion about the step cap.
OPEN = rule.Rule(max_step_pct=Decimal("100"), floor_pct=Decimal("90"),
                 ceiling_pct=Decimal("500"), round_to=10)


def _series(room_type_id: int, excl: str, night: date, available: bool = True,
            source_id: int = BOOKING_COM, tax: str | None = "0"):
    """``tax`` of None is a site that printed no tax line at all."""
    return SimpleNamespace(
        room_type_id=room_type_id, is_available=available, source_id=source_id,
        last_price_exclusive=Decimal(excl),
        last_taxes_fees=None if tax is None else Decimal(tax),
        last_price_inclusive=None, currency="INR",
        check_in=night, check_out=night + timedelta(days=1),
    )


def _rows(night: date = date(2026, 9, 21), *, ours: str = "6358",
          sterling: str | None = "2460", sold_out: bool = False,
          sterling_direct: str | None = None):
    """One night. ``sterling_direct`` adds their own-site listing as well."""
    rows = [
        (_series(OUR_CLASSIC, ours, night), OURS, "Standard Double Room"),
        (_series(OUR_DELUXE, "5610", night), OURS, "Deluxe Double Room"),
        (_series(104, "8000", night), MGM, "Classic Room - King Bed"),
    ]
    if sterling is not None:
        rows.append((_series(110, sterling, night, available=not sold_out), STERLING, "Classic Room"))
        rows.append((_series(111, "2936", night, available=not sold_out), STERLING,
                     "Mountain View Classic Room"))
    if sterling_direct is not None:
        rows.append((_series(112, sterling_direct, night, source_id=STERLING_DIRECT),
                     STERLING, "Classic Room"))
    return rows


def _history(nights: int = 6, prices=None):
    out = []
    for i in range(nights):
        night = date(2026, 9, 21) - timedelta(days=nights - i)
        out += _rows(night, sterling=(prices[i] if prices else "2460"))
    return out


def _for(rows, room_type_id: int = OUR_CLASSIC, *, benchmark=STERLING_ONLY, rule_=OPEN):
    (p,) = [x for x in rule.propose(rows, own_hotel_id=9, rule=rule_, benchmark=benchmark)
            if x.room_type_id == room_type_id]
    return p


class TestTheRuleFollowsOneHotel:
    def test_the_target_is_their_price_less_the_gap(self):
        p = _for(_rows(sterling="2460"))
        assert p.market == Decimal("2460")
        assert p.benchmark == "Sterling"
        assert p.target == Decimal("2360")

    def test_they_go_up_and_we_go_up(self):
        """The owner's example: 1,000 to 1,300 means we set 1,200."""
        before = _for(_rows(ours="900", sterling="1000"))
        after = _for(_rows(ours="900", sterling="1300"))
        assert before.target == Decimal("900")
        assert after.target == Decimal("1200")

    def test_they_come_down_and_we_come_down(self):
        """And 1,300 to 1,200 means we set 1,100."""
        before = _for(_rows(ours="1200", sterling="1300"))
        after = _for(_rows(ours="1200", sterling="1200"))
        assert before.target == Decimal("1200")
        assert after.target == Decimal("1100")

    def test_the_gap_is_rupees_so_it_does_not_widen_when_they_move(self):
        """Against ``wanted``, the rule's own answer, because the step cap
        below is a limit on how far OUR price may move in a night and has
        nothing to say about the gap."""
        cheap = _for(_rows(ours="900", sterling="1000"))
        dear = _for(_rows(ours="900", sterling="9000"))
        assert cheap.market - cheap.wanted == Decimal("100")
        assert dear.market - dear.wanted == Decimal("100")

    def test_the_gap_is_exact_and_not_rounded_away(self):
        """Rounding a 2,936 benchmark to the nearest ten is ninety-six less,
        not a hundred less, and the gap is the instruction."""
        p = _for(_rows(sterling="2936"), rule_=rule.Rule(
            max_step_pct=Decimal("100"), floor_pct=Decimal("90"),
            ceiling_pct=Decimal("500"), round_to=100))
        assert p.wanted == Decimal("2836")

    def test_their_entry_price_is_the_cheapest_room_of_the_tier(self):
        """Sterling sells a Classic at 2,460 and a Mountain View at 2,936."""
        p = _for(_rows())
        assert p.benchmark_entry.room == "Classic Room"
        assert p.benchmark_entry.price == Decimal("2460")

    def test_a_tier_they_do_not_sell_gets_no_proposal_and_says_so(self):
        """Sterling has no deluxe room. Ours is not priced off a classic one."""
        p = _for(_rows(), room_type_id=OUR_DELUXE)
        assert p.target is None
        assert p.benchmark == "Sterling"
        assert "no Deluxe on sale tonight" in p.held

    def test_a_sold_out_benchmark_is_not_a_price_to_follow(self):
        """The median deliberately keeps a full hotel in at its last figure.
        Here that figure is the whole input, and it has stopped being a rate
        anybody can book."""
        p = _for(_rows(sold_out=True))
        assert p.target is None
        assert "on sale tonight" in p.held

    def test_one_competitor_is_not_too_few_competitors(self):
        """min_competitors refuses a market of one nobody chose. This is a
        market of one chosen on purpose."""
        strict = rule.Rule(min_competitors=5, max_step_pct=Decimal("100"),
                           floor_pct=Decimal("90"), ceiling_pct=Decimal("500"))
        p = _for(_rows(), rule_=strict)
        assert p.target == Decimal("2360")
        assert p.held is None


class TestTheSiteMustMatchOnBothSides:
    """Sterling is on Booking.com at 2,931 and on its own engine at 2,460."""

    def test_their_other_sites_price_is_not_the_one_followed(self):
        p = _for(_rows(sterling="2931", sterling_direct="2460"))
        assert p.benchmark_entry.source_id == BOOKING_COM
        assert p.market == Decimal("2931")
        assert p.target == Decimal("2831")

    def test_a_night_our_site_did_not_report_them_is_no_proposal(self):
        """Not a fallback to whatever else they are listed on: that would
        price our Booking.com room against their direct-booking discount."""
        p = _for(_rows(sterling=None, sterling_direct="2460"))
        assert p.target is None
        assert "on the site our own price comes from" in p.held

    def test_the_median_still_takes_them_at_whatever_they_quote(self):
        """The site only matters to the benchmark. To a market figure a
        competitor sits at roughly that level either way."""
        p = _for(_rows(sterling="2931", sterling_direct="2460"), benchmark=None)
        assert p.market == rule.market_figure([Decimal("2460"), Decimal("8000")])


class TestTheLimitsStillApply:
    def test_a_long_way_down_is_still_one_step(self):
        """Ours 6,358, theirs 2,460: the rule wants 2,360 and the step cap
        allows 10% tonight. The cap is what stops a competitor's bad reading
        costing a cliff.

        5,730 and not 5,720: the exact cap is 5,722.2, and rounding to the
        nearest ten used to give 5,720 -- a 10.03% cut, past the 10% cap it
        was measuring. A capped move now rounds back towards today's price
        so it can never breach its own bound."""
        p = _for(_rows(), rule_=rule.Rule(max_step_pct=Decimal("10"), round_to=10))
        assert p.wanted == Decimal("2360")
        assert p.target == Decimal("5730")
        assert (Decimal("6358") - p.target) / Decimal("6358") <= Decimal("0.10")
        assert "capped at -10%" in p.capped

    def test_past_the_floor_is_held_not_written(self):
        p = _for(_rows(), rule_=rule.Rule(max_step_pct=Decimal("100"),
                                          floor_pct=Decimal("30"), round_to=10))
        assert p.held and "below the floor" in p.held

    def test_a_room_already_moved_tonight_follows_them_again(self):
        """THE ONE LIMIT THAT DOES NOT APPLY HERE, and deliberately.

        Under the median rule a room that moved is held for the night: the
        target is an aim at a wobbling figure, and a room free to chase it
        all evening walks on noise. A follow is an instruction about one
        named hotel. If Sterling move at four o'clock, "tomorrow" is not an
        answer -- it leaves us hundreds off their price for the rest of the
        day, which is the opposite of the rule.
        """
        (p,) = [x for x in rule.propose(_rows(), own_hotel_id=9, rule=OPEN,
                                        benchmark=STERLING_ONLY, moved_today={OUR_CLASSIC})
                if x.room_type_id == OUR_CLASSIC]
        assert p.held is None
        assert p.target == p.wanted == Decimal("2360")

    def test_the_median_rule_is_still_held_for_the_night(self):
        """The lock is not removed, it is scoped: the rule it was written for
        still has it."""
        rows = [
            (_series(OUR_CLASSIC, "6358", date(2026, 9, 24)), OURS, "Standard Double Room"),
            (_series(110, "4343", date(2026, 9, 24)), STERLING, "Classic Room"),
            (_series(111, "4300", date(2026, 9, 24)), MGM, "Classic Room"),
        ]
        (p,) = [x for x in rule.propose(rows, own_hotel_id=9, rule=OPEN,
                                        usual={OUR_CLASSIC: (Decimal("1.45"), 7)},
                                        moved_today={OUR_CLASSIC})
                if x.room_type_id == OUR_CLASSIC]
        assert p.held and "already moved once tonight" in p.held


class TestTheDemandLiftsAreOff:
    def test_a_weekend_does_not_move_the_gap(self):
        """A Friday lift makes the gap something other than the hundred
        rupees the owner set."""
        weekend = rule.Rule(max_step_pct=Decimal("100"), floor_pct=Decimal("90"),
                            ceiling_pct=Decimal("500"), weekend_pct=Decimal("15"), round_to=10)
        (p,) = [x for x in rule.propose(_rows(date(2026, 9, 25)), own_hotel_id=9,
                                        rule=weekend, benchmark=STERLING_ONLY,
                                        night=date(2026, 9, 25))
                if x.room_type_id == OUR_CLASSIC]
        assert p.target == Decimal("2360")
        assert p.demand_pct == Decimal("0")
        assert p.demand_note is None

    def test_the_owners_standing_lean_does_not_apply_either(self):
        leaned = rule.Rule(position_pct=Decimal("-5"), max_step_pct=Decimal("100"),
                           floor_pct=Decimal("90"), ceiling_pct=Decimal("500"), round_to=10)
        assert _for(_rows(), rule_=leaned).target == Decimal("2360")


class TestWithNoBenchmarkNothingChanges:
    def test_the_median_rule_is_exactly_as_it_was(self):
        p = _for(_rows(), benchmark=None)
        assert {c.hotel for c in p.competitors} == {"Sterling", "MGM Winds"}
        assert p.market == rule.market_figure([Decimal("2460"), Decimal("8000")])
        assert p.benchmark is None
        assert p.benchmark_entry is None


# ---------------------------------------------------------------------------
# The AI column, which reads the same hotel the price was computed from.
# ---------------------------------------------------------------------------


def _ask(**over) -> Ask:
    base = dict(
        hotel="ASG HOLIDAY RESORTS", room_name="Standard Double Room", tier_label="Classic",
        check_in=date(2026, 9, 21), our_price=Decimal("6358"),
        competitors=(("Sterling", "Classic Room", Decimal("2460")),),
        rule_position_pct=Decimal("0"), benchmark="Sterling",
        benchmark_gap=Decimal("2.5846"),
        benchmark_recent=((date(2026, 9, 18), Decimal("2800")),
                          (date(2026, 9, 19), Decimal("2630")),
                          (date(2026, 9, 20), Decimal("2460"))),
    )
    base.update(over)
    return Ask(**base)


class TestTheModelSeesTheSameHotel:
    def test_the_prompt_names_the_hotel_and_never_the_others(self):
        prompt = _ask().as_prompt()
        assert "Sterling" in prompt
        assert "MGM" not in prompt
        assert "median" not in prompt.lower()

    def test_the_prompt_carries_how_the_benchmark_moved(self):
        prompt = _ask().as_prompt()
        assert "18 Sep 2,800" in prompt and "20 Sep 2,460" in prompt

    def test_the_anchor_is_our_gap_over_that_hotel(self):
        """2,460 x 2.5846 is 6,358 -- our price, which is the point."""
        prompt = _ask().as_prompt()
        assert "+158% above Sterling's entry price" in prompt
        assert "usual level near 6,358" in prompt

    def test_a_benchmark_with_no_history_says_so_rather_than_guessing(self):
        prompt = _ask(benchmark_gap=None).as_prompt()
        assert "not enough history" in prompt
        assert "usual level near" not in prompt

    def test_the_brief_changes_with_the_basis(self):
        """One price carries no consensus, so the model is told a different thing."""
        seen = {}

        def complete(*, system, user, schema, model):
            seen["system"] = system
            return json.dumps({"position_pct": 0, "confidence": 0.5,
                               "rationale": "steady", "key_factors": []})

        advise(_ask(), complete=complete, model="m", max_position_pct=Decimal("8"))
        assert seen["system"] is SYSTEM_BENCHMARK
        advise(_ask(benchmark=None), complete=complete, model="m", max_position_pct=Decimal("8"))
        assert seen["system"] is SYSTEM

    def test_the_row_it_is_shown_is_the_row_the_price_came_from(self):
        """Not a second narrowing by hotel name: that would hand the model
        their direct rate while the price beside it used Booking.com's."""
        p = _for(_rows(sterling="2931", sterling_direct="2460"))
        assert p.benchmark_entry.price == Decimal("2931")
        assert p.benchmark_entry.source_id == BOOKING_COM

    def test_the_model_still_cannot_name_a_price(self):
        got = advise(
            _ask(),
            complete=lambda **kw: json.dumps({
                "position_pct": -40, "confidence": 0.9,
                "rationale": "set it to 3000", "key_factors": [],
            }),
            model="m", max_position_pct=Decimal("8"),
        )
        assert got.position_pct == Decimal("-8")
        assert got.clamped
        assert not hasattr(Advice, "target")


class TestTheGapIsMeasuredAgainstThatHotel:
    def test_it_is_our_price_over_theirs_not_over_the_median(self):
        """Ours 6,358; Sterling 2,460; MGM 8,000. The median of the two is
        5,230, and a gap of 1.22 pointed at Sterling's 2,460 would read as a
        rate to cut by half."""
        gaps = rule.usual_gaps_against(_history(), own_hotel_id=9, benchmark_hotel_id=10)
        gap, nights = gaps[OUR_CLASSIC]
        assert nights == 6
        assert round(gap, 2) == Decimal("2.58")

        median_gap, _ = rule.usual_gaps(_history(), own_hotel_id=9)[OUR_CLASSIC]
        assert round(median_gap, 2) == Decimal("1.22")

    def test_a_benchmark_added_this_week_has_no_gap_yet(self):
        """MIN_HISTORY_NIGHTS still applies: four nights is not a usual place."""
        gaps = rule.usual_gaps_against(_history(nights=4), own_hotel_id=9, benchmark_hotel_id=10)
        assert OUR_CLASSIC not in gaps

    def test_a_tier_the_benchmark_never_sold_has_no_gap(self):
        gaps = rule.usual_gaps_against(_history(), own_hotel_id=9, benchmark_hotel_id=10)
        assert OUR_DELUXE not in gaps


class TestTheMovementLine:
    def test_it_is_that_hotels_entry_price_night_by_night_oldest_first(self):
        rows = _history(prices=["2800", "2800", "2700", "2630", "2540", "2460"])
        moved = rule.benchmark_history(rows, own_hotel_id=9, benchmark_hotel_id=10, tier="classic")
        assert [p for _, p in moved] == [Decimal(p) for p in
                                         ("2800", "2800", "2700", "2630", "2540", "2460")]
        assert [n for n, _ in moved] == sorted(n for n, _ in moved)

    def test_a_night_it_was_full_is_a_gap_not_a_repeated_price(self):
        """``_night`` keeps a sold-out hotel in the market at its last figure.
        In a series of one that reads as "they held their price" on the night
        they had nothing to sell."""
        rows = _rows(date(2026, 9, 19)) + _rows(date(2026, 9, 20), sold_out=True)
        moved = rule.benchmark_history(rows, own_hotel_id=9, benchmark_hotel_id=10, tier="classic")
        assert [n for n, _ in moved] == [date(2026, 9, 19)]

    def test_only_the_last_few_nights_are_carried(self):
        rows = _history(nights=14)
        moved = rule.benchmark_history(rows, own_hotel_id=9, benchmark_hotel_id=10,
                                       tier="classic", nights=8)
        assert len(moved) == 8


class TestThePageSaysWhatItIsComparing:
    """A column still headed "Market (median)" would be telling the owner the
    number came from ten hotels when it came from one."""

    def _render(self, **over) -> str:
        from app.dashboard.routes import templates
        ctx = dict(
            request=SimpleNamespace(url=SimpleNamespace(path="/repricing")),
            user=SimpleNamespace(id=2, username="owner", is_admin=False),
            settings=SimpleNamespace(
                auto_enabled=False, position_pct=0, max_step_pct=10, floor_pct=30,
                ceiling_pct=50, min_competitors=2, round_to=10, channel="Booking.com",
                weekend_pct=0, sold_out_pct=10, known_channels=[], benchmark_hotel_id=10,
                benchmark_undercut=Decimal("100"),
            ),
            own=SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS"),
            rooms=[], mappings={}, proposals=[_for(_rows())],
            check_in=date(2026, 9, 21), actions=[], latest_rms={}, advice={}, ai_wanted={},
            channel_rms={}, other_channels=[], has_rate_app=True, rule=rule,
            competitors=[SimpleNamespace(id=10, name="Sterling"),
                         SimpleNamespace(id=4, name="MGM Winds")],
            benchmark=SimpleNamespace(id=10, name="Sterling"),
        )
        ctx.update(over)
        return templates.get_template("repricing.html").render(**ctx)

    def test_the_rules_settings_are_not_on_this_page(self):
        """The owner asked for the panel to go: there is one rule, and nine
        boxes for shaping a rule nobody is running was the page's largest
        thing and its least used. The settings are unchanged and still served
        by the API -- only the form is gone."""
        html = self._render()
        assert 'id="repricing-settings"' not in html
        assert "Save rule" not in html
        assert '<option value="" >every competitor' not in html

    def test_the_hotel_being_followed_is_still_named(self):
        """Removing the picker must not remove the ANSWER to "who are we
        priced against". A page that quietly compares against somebody it
        will not name is worse than one with a settings panel."""
        html = self._render()
        assert "Sterling" in html

    def test_the_market_column_is_headed_with_the_hotel_it_holds(self):
        html = self._render()
        assert "Market (median)" not in html
        assert "Sterling" in html

    def test_with_no_benchmark_it_is_the_median_again(self):
        html = self._render(benchmark=None, proposals=[_for(_rows(), benchmark=None)])
        assert "Market (median)" in html

    def test_the_subtraction_is_shown_not_just_its_answer(self):
        assert "2,360" in self._render()


class TestTheGapIsOnWhatTheGuestPays:
    """The owner's worked example, in their own numbers:

        Sterling Booking.com   4,146 + 207 tax = 4,353
        our target                             = 4,253   (a hundred under)

    The gap is on the line a guest reads, not on the rate underneath it.
    That is not presentation: the two tax rates are different, so the two
    bases give different answers and only one of them is the owner's rule.
    """

    WITH_TAX = rule.Benchmark(hotel_id=10, hotel="Sterling", undercut=Decimal("100"))

    def _rows(self, night=date(2026, 9, 24)):
        # Real proportions: ours taxed at 5.68%, theirs at 5.0%.
        return [
            (_series(OUR_CLASSIC, "6358", night, tax="361"), OURS, "Standard Double Room"),
            (_series(110, "4146", night, tax="207"), STERLING, "Classic Room"),
        ]

    def _one(self, rows, benchmark):
        (p,) = [x for x in rule.propose(rows, own_hotel_id=9, rule=OPEN, benchmark=benchmark)
                if x.room_type_id == OUR_CLASSIC]
        return p

    def test_their_price_is_the_one_the_guest_is_quoted(self):
        p = self._one(self._rows(), self.WITH_TAX)
        assert p.market == Decimal("4353")

    def test_the_target_is_a_hundred_under_that(self):
        p = self._one(self._rows(), self.WITH_TAX)
        assert p.wanted == Decimal("4253")

    def test_our_own_price_is_quoted_on_the_same_footing(self):
        """6,358 + 361. Subtracting from one basis and applying to the other
        is how a hundred rupees becomes several hundred."""
        p = self._one(self._rows(), self.WITH_TAX)
        assert p.our_price == Decimal("6719")

    def test_the_two_bases_do_not_agree_which_is_the_whole_point(self):
        pre_tax = self._one(self._rows(), rule.Benchmark(
            hotel_id=10, hotel="Sterling", undercut=Decimal("100"), with_tax=False))
        with_tax = self._one(self._rows(), self.WITH_TAX)
        assert pre_tax.wanted == Decimal("4046")
        assert with_tax.wanted == Decimal("4253")

    def test_a_site_that_printed_no_tax_stops_the_proposal(self):
        """displayed_price falls back to the rate and marks it. Taking a
        hundred off an all-in price to set a pre-tax one is a gap of several
        hundred wearing the number the owner typed."""
        night = date(2026, 9, 24)
        rows = [
            (_series(OUR_CLASSIC, "6358", night, tax="361"), OURS, "Standard Double Room"),
            (_series(110, "4146", night, tax=None), STERLING, "Classic Room"),
        ]
        p = self._one(rows, self.WITH_TAX)
        assert p.target is None
        assert "did not publish its tax" in p.held
        assert "Sterling" in p.held

    def test_the_rms_rate_is_worked_back_from_the_all_in_target(self):
        """The owner's question: what do we type into RMS? Their RMS holds
        10,710 while Booking.com quotes 6,719 all-in, so the guest sees
        0.6274 of the RMS rate, and 4,253 needs 6,779."""
        p = self._one(self._rows(), self.WITH_TAX)
        got = rule.to_rms(p.wanted, p.our_price, Decimal("10710"), round_to=1)
        assert got == Decimal("6779")
        # ...and it converts straight back to the target the owner wrote down.
        assert (got * p.our_price / Decimal("10710")).quantize(Decimal("1")) == Decimal("4253")


class TestEachRoomIsPairedWithOneOfTheirs:
    """The rooms that compete are named "Deluxe Double Room" here and
    "Classic room" there. No reading of those names pairs them, so the owner
    says once and the rule follows it."""

    def _rows(self, night=date(2026, 9, 24)):
        return [
            (_series(OUR_CLASSIC, "6358", night), OURS, "Standard Double Room"),
            (_series(OUR_DELUXE, "5610", night), OURS, "Deluxe Double Room"),
            (_series(110, "4343", night), STERLING, "Classic room"),
            (_series(112, "4973", night), STERLING, "Mountain View Classic Room"),
        ]

    def _pairs(self, over=None):
        pairs = {OUR_DELUXE: "Classic room", OUR_CLASSIC: "Mountain View Classic Room"}
        pairs.update(over or {})
        return rule.Benchmark(hotel_id=10, hotel="Sterling", undercut=Decimal("100"),
                              with_tax=False, room_pairs=pairs)

    def _one(self, room_type_id, benchmark=None, rows=None):
        (p,) = [x for x in rule.propose(rows or self._rows(), own_hotel_id=9, rule=OPEN,
                                        benchmark=benchmark or self._pairs())
                if x.room_type_id == room_type_id]
        return p

    def test_each_room_follows_the_room_it_was_paired_with(self):
        """Not the cheapest of theirs for both -- each its own."""
        assert self._one(OUR_DELUXE).market == Decimal("4343")
        assert self._one(OUR_CLASSIC).market == Decimal("4973")

    def test_the_tier_is_overruled_not_consulted(self):
        """Our deluxe-tier room follows their classic-tier one, which is the
        whole reason the pairing exists."""
        p = self._one(OUR_DELUXE)
        assert p.tier == "deluxe"
        assert p.benchmark_entry.room == "Classic room"
        assert p.target == Decimal("4243")

    def test_a_room_nobody_paired_is_priced_by_nobody(self):
        """An unchosen select drops the key, the way the form posts it."""
        bench = rule.Benchmark(hotel_id=10, hotel="Sterling", undercut=Decimal("100"),
                               with_tax=False, room_pairs={OUR_DELUXE: "Classic room"})
        p = self._one(OUR_CLASSIC, benchmark=bench)
        assert p.target is None
        assert "not paired" in p.held

    def test_a_renamed_or_recased_room_still_matches(self):
        """The owner picked the name off a list the site produced, and the
        site then writes it differently next week."""
        bench = self._pairs({OUR_DELUXE: "CLASSIC ROOM"})
        assert self._one(OUR_DELUXE, benchmark=bench).market == Decimal("4343")

    def test_their_room_missing_tonight_holds_and_names_it(self):
        """It does not slide across to whichever other room of theirs is
        cheapest -- the owner named this one."""
        rows = [r for r in self._rows() if r[2] != "Classic room"]
        p = self._one(OUR_DELUXE, rows=rows)
        assert p.target is None
        assert '"Classic room"' in p.held

    def test_the_gap_is_still_a_hundred_under_the_paired_room(self):
        for room_type_id in (OUR_DELUXE, OUR_CLASSIC):
            p = self._one(room_type_id)
            assert p.market - p.wanted == Decimal("100")


class TestARoomWithNoRateOnThatBoardStillHasARow:
    """The board filter drops the rows before the rooms are grouped, so a
    room sold only room-only never reaches the rule at all. Left alone it
    vanishes off the page -- the one outcome this system refuses everywhere
    else, because a rate nobody can see is a rate nobody can fix."""

    BOARD = rule.Benchmark(hotel_id=10, hotel="Sterling", undercut=Decimal("100"),
                           with_tax=False, meal_plan="Breakfast",
                           room_pairs={OUR_CLASSIC: "Classic room"})

    def _rows(self, night=date(2026, 9, 24)):
        def s(rt, excl, plan):
            ser = _series(rt, excl, night)
            ser.meal_plan = plan
            return ser
        return [
            (s(OUR_CLASSIC, "6358", "Room Only"), OURS, "Standard Double Room"),
            (s(OUR_DELUXE, "5984", "Breakfast"), OURS, "Deluxe Double Room"),
            (s(110, "4343", "Breakfast"), STERLING, "Classic room"),
        ]

    def _all(self):
        return {p.room_name: p for p in
                rule.propose(self._rows(), own_hotel_id=9, rule=OPEN, benchmark=self.BOARD)}

    def test_the_room_is_still_on_the_page(self):
        assert "Standard Double Room" in self._all()

    def test_it_says_which_rates_are_missing_and_whose_it_would_face(self):
        """With no fallback board configured there is nowhere to drop to."""
        p = self._all()["Standard Double Room"]
        assert p.target is None
        assert "neither breakfast" in p.held
        assert "Classic room" in p.held

    def test_a_room_that_does_have_the_board_is_priced_as_normal(self):
        """Unpaired here, so held for that reason and not the board one."""
        p = self._all()["Deluxe Double Room"]
        assert p.our_price == Decimal("5984")
        assert "not paired" in p.held


class TestARoomWithoutBreakfastDropsToRoomOnly:
    """Booking.com sells ASG's Standard Double room-only and nothing else.
    Pinning breakfast left it unpriced -- and a room the owner competes with
    every night is not a room to leave unpriced.

    BOTH SIDES DROP TOGETHER. Our room-only rate against their breakfast one
    is the cross-board mistake the board setting exists to prevent; on 24 Sep
    it would have reported us 1,350 cheaper than we were.
    """

    BENCH = rule.Benchmark(
        hotel_id=10, hotel="Sterling", undercut=Decimal("100"), with_tax=False,
        meal_plan="Breakfast", fallback_board="Room Only",
        room_pairs={OUR_CLASSIC: "Mountain View Classic Room",
                    OUR_DELUXE: "Classic room"},
    )

    def _rows(self, night=date(2026, 9, 24)):
        def s(rt, excl, plan, name, hotel):
            ser = _series(rt, excl, night)
            ser.meal_plan = plan
            return (ser, hotel, name)
        return [
            # ours: the deluxe sells both boards, the standard only room-only
            s(OUR_DELUXE, "5610", "Room Only", "Deluxe Double Room", OURS),
            s(OUR_DELUXE, "5984", "Breakfast", "Deluxe Double Room", OURS),
            s(OUR_CLASSIC, "6358", "Room Only", "Standard Double Room", OURS),
            # theirs: both boards on both rooms
            s(110, "4343", "Room Only", "Classic room", STERLING),
            s(110, "5693", "Breakfast", "Classic room", STERLING),
            s(112, "4973", "Room Only", "Mountain View Classic Room", STERLING),
            s(112, "6323", "Breakfast", "Mountain View Classic Room", STERLING),
        ]

    def _all(self):
        return {p.room_name: p for p in
                rule.propose(self._rows(), own_hotel_id=9, rule=OPEN, benchmark=self.BENCH)}

    def test_the_room_without_breakfast_is_priced_after_all(self):
        p = self._all()["Standard Double Room"]
        assert p.board == "Room Only"
        assert p.target == Decimal("4873")

    def test_it_is_compared_against_their_room_only_rate_not_their_breakfast_one(self):
        """4,973 is their Mountain View room-only. Their breakfast rate for
        the same room is 6,323, and using it would put us 1,350 too high."""
        p = self._all()["Standard Double Room"]
        assert p.market == Decimal("4973")
        assert p.benchmark_entry.room == "Mountain View Classic Room"

    def test_a_room_that_has_breakfast_still_uses_breakfast(self):
        p = self._all()["Deluxe Double Room"]
        assert p.board == "Breakfast"
        assert p.our_price == Decimal("5984")
        assert p.market == Decimal("5693")
        assert p.target == Decimal("5593")

    def test_both_rooms_end_up_a_hundred_under_their_own_rival(self):
        """The whole point: whatever Sterling does, both of ours sit 100 under."""
        for room in ("Standard Double Room", "Deluxe Double Room"):
            p = self._all()[room]
            assert p.market - p.target == Decimal("100")


class TestACappedMoveNeverBreachesTheBoundThatCappedIt:
    """A room at 6,719 with a 30% cap may go to 4,703.3. Rounded to the
    nearest ten that is 4,700 -- three rupees past the cap. Where the floor
    is the same 30%, those three rupees turned a legitimate move into
    "held: below the floor of 4,703" on a row showing 4,700, which reads as
    arithmetic nobody can argue with and is really a rounding artefact."""

    RULE = rule.Rule(max_step_pct=Decimal("30"), floor_pct=Decimal("30"),
                     ceiling_pct=Decimal("50"), round_to=10)

    def test_the_downward_cap_lands_inside_the_floor(self):
        target, held, capped = rule.guard(Decimal("6719"), Decimal("4241"), self.RULE)
        assert target == Decimal("4710")
        assert capped and "capped at -30%" in capped
        assert held is None

    def test_the_upward_cap_lands_inside_the_ceiling(self):
        tight = rule.Rule(max_step_pct=Decimal("50"), floor_pct=Decimal("30"),
                          ceiling_pct=Decimal("50"), round_to=10)
        target, held, capped = rule.guard(Decimal("6719"), Decimal("99999"), tight)
        assert target <= Decimal("6719") * Decimal("1.5")
        assert held is None

    def test_it_gives_up_at_most_one_rounding_step_of_the_move(self):
        target, _, _ = rule.guard(Decimal("6719"), Decimal("4241"), self.RULE)
        assert Decimal("6719") - target >= Decimal("6719") * Decimal("0.3") - 10

    def test_a_move_inside_the_cap_is_untouched(self):
        target, held, capped = rule.guard(Decimal("6324"), Decimal("4869"), self.RULE)
        assert target == Decimal("4869")
        assert capped is None and held is None


class TestTheRateWeWriteKeepsTheWholeGap:
    """Tonight's Deluxe Double, as the owner found it.

        Sterling's Classic Room   5,316
        the rule's target         5,216   (a hundred under -- correct)
        written into RMS          9,290
        shown to the guest        5,218   (ninety-eight under -- not the rule)

    The target refuses to round itself, because the gap is an instruction
    and not an aim. Then ``to_rms`` rounded it anyway: the ratio here is
    about 0.56, so the ten rupees of tidiness the owner asked for on the RMS
    grid came back out as two on the line they compete on. The setting is
    cosmetic; the gap is not, so the conversion of an instruction goes to
    the rupee, and downwards -- a rupee further under Sterling keeps the
    promise, a rupee over breaks it.
    """

    #: RMS holds 11,260 where Booking.com quotes 6,324 all-in.
    OUR_PRICE, CURRENT_RMS = Decimal("6324"), Decimal("11260")

    def _guest_sees(self, rms):
        """The trip back, the way the page and the site both make it."""
        return (rms * self.OUR_PRICE / self.CURRENT_RMS).quantize(Decimal("1"))

    def test_the_rate_comes_back_out_as_the_target(self):
        rms = rule.to_rms(Decimal("5216"), self.OUR_PRICE, self.CURRENT_RMS,
                          round_to=10, exact=True)
        assert self._guest_sees(rms) == Decimal("5216")

    def test_the_gap_is_the_hundred_that_was_set(self):
        rms = rule.to_rms(Decimal("5216"), self.OUR_PRICE, self.CURRENT_RMS,
                          round_to=10, exact=True)
        assert Decimal("5316") - self._guest_sees(rms) == Decimal("100")

    def test_the_owners_rounding_is_what_used_to_lose_it(self):
        """Not a regression guard on the old answer -- the record of what the
        two rupees were, so nobody reintroduces the tidiness by helpfully
        passing ``round_to`` through again."""
        tidy = rule.to_rms(Decimal("5216"), self.OUR_PRICE, self.CURRENT_RMS, round_to=10)
        assert tidy == Decimal("9290")
        assert self._guest_sees(tidy) == Decimal("5218")

    def test_it_lands_under_the_target_and_never_over(self):
        """Every rate in the range, against a target that divides badly."""
        for rms_now in range(9000, 12000, 37):
            rms = rule.to_rms(Decimal("5216"), self.OUR_PRICE, Decimal(rms_now),
                              round_to=10, exact=True)
            assert rms * self.OUR_PRICE / Decimal(rms_now) <= Decimal("5216")

    def test_a_benchmark_proposal_is_marked_as_an_instruction(self):
        rows = [
            (_series(OUR_CLASSIC, "6358", date(2026, 9, 24)), OURS, "Standard Double Room"),
            (_series(110, "4343", date(2026, 9, 24)), STERLING, "Classic Room"),
        ]
        (p,) = [x for x in rule.propose(rows, own_hotel_id=9, rule=OPEN, benchmark=STERLING_ONLY)
                if x.room_type_id == OUR_CLASSIC]
        assert p.exact is True

    def test_so_is_a_price_the_owner_typed(self):
        """Their own number is the most exact instruction there is. The
        median rule's proposals are the only approximate thing here."""
        rows = [(_series(OUR_CLASSIC, "6358", date(2026, 9, 24)), OURS, "Standard Double Room"),
                (_series(110, "4343", date(2026, 9, 24)), MGM, "Classic Room"),
                (_series(111, "4300", date(2026, 9, 24)), STERLING, "Classic Room")]
        (p,) = [x for x in rule.propose(rows, own_hotel_id=9, rule=OPEN)
                if x.room_type_id == OUR_CLASSIC]
        assert p.exact is False
        assert rule.override(p, Decimal("5000")).exact is True


class TestAFollowThatHasCaughtUpSaysNothing:
    """Sitting a hundred under them, the rule recomputes the same rate every
    half hour. The row offered a "proposed price" identical to today's, at
    +0.0%, under a green "will apply" -- furniture for a decision nobody has
    to make, and an invitation to sign into RMS to write the number already
    there. Tonight it was worse than idle: the trip from a guest price to an
    RMS rate and back rounds twice, so the settled Deluxe proposed 5,845 over
    its own 5,844 and would have logged in to move a rate by one rupee.

    The threshold is the owner's ``round_to``, not zero and not a constant
    invented here: it is their own statement of the smallest step a rate
    should take.
    """

    def test_the_same_rate_is_settled(self):
        assert rule.settled(Decimal("5082"), Decimal("5082"), round_to=10)

    def test_and_so_is_one_a_rupee_off_after_the_round_trip(self):
        assert rule.settled(Decimal("5844"), Decimal("5845"), round_to=10)

    def test_a_real_move_is_not(self):
        """Sterling moved; the rule has something to say again."""
        assert not rule.settled(Decimal("5082"), Decimal("5657"), round_to=10)

    def test_the_owner_sets_how_small_is_too_small(self):
        """round_to 1 means every rupee counts, and the same pair is a move."""
        assert not rule.settled(Decimal("5844"), Decimal("5845"), round_to=1)

    def test_a_cell_never_read_is_not_settled(self):
        """No reading is not "nothing to do" -- it is "we do not know yet"."""
        assert not rule.settled(None, Decimal("5845"), round_to=10)
        assert not rule.settled(Decimal("5844"), None, round_to=10)

    def test_a_step_of_zero_cannot_swallow_every_move(self):
        """round_to 0 would make abs(diff) < 0 impossible -- guarded, so a
        bad setting cannot silently stop the rule writing anything."""
        assert not rule.settled(Decimal("5844"), Decimal("5845"), round_to=0)
        assert rule.settled(Decimal("5844"), Decimal("5844"), round_to=0)
