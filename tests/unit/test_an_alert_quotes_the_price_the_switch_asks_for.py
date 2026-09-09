"""The Settings tax switch decides what a WhatsApp says, not just what a screen shows.

WHAT WAS WRONG
==============
"Are we sending prices with tax or without?" had no answer on any page. The
messages quoted ``price_changes.old_price`` and ``new_price`` verbatim, both
stored on ``PRICE_BASIS`` -- exclusive here -- and said nothing about it. So:

* a manager who turned the matrix to all-in rates got a WhatsApp quoting
  pre-tax ones. One room, two numbers, no way to tell which was which.
* the three Treebo properties, which publish no pre-tax figure at all, reached
  the same message as all-in rates through the documented fallback in
  ``NormalizedOffer.price_on`` -- sitting unlabelled beside seven pre-tax ones.

THE RULE, WHICH IS THE MATRIX'S RULE
====================================
One switch, obeyed everywhere, through one implementation
(``services/price_display``). Show the component asked for; where the site did
not publish it, show what it did and MARK it; never infer a tax rate. What is
new here is that a MOVE has two sides, and both of them have to land on the
same basis before the difference between them means anything.

WHAT THE MESSAGE MUST NEVER DO
==============================
Print three numbers that do not add up. ``9,000 -> 9,500, up 500`` is right on
the pre-tax basis and wrong the moment the same line reads ``10,620 ->
11,210``, where the move is 590. A reader who checks the arithmetic once and
finds it wrong stops trusting the figures, which is worse than either basis.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.notifications.base import ChangeLine
from app.notifications.render import render_digest, render_summary
from app.services.price_display import (
    EXCLUSIVE_NOTE,
    INCLUSIVE_NOTE,
    MIXED_NOTE,
    Components,
    displayed_move,
)
from app.workers.tasks_notify import _render_lines


def side(exclusive=None, taxes=None, inclusive=None, stored=None):
    """One side of a move, as ``price_changes`` records it."""
    return Components(
        current_price=stored,
        last_price_exclusive=exclusive,
        last_taxes_fees=taxes,
        last_price_inclusive=inclusive,
    )


#: Booking.com and Cleartrip: the room and its tax, stated separately. The
#: same 9,000/1,620 pair the matrix test uses, moving to 9,500/1,710.
SPLIT_OLD = side(exclusive=Decimal("9000"), taxes=Decimal("1620"), stored=Decimal("9000"))
SPLIT_NEW = side(exclusive=Decimal("9500"), taxes=Decimal("1710"), stored=Decimal("9500"))

#: Treebo: one all-in figure and no pre-tax number published anywhere.
ALL_IN_OLD = side(inclusive=Decimal("4822"), stored=Decimal("4822"))
ALL_IN_NEW = side(inclusive=Decimal("5100"), stored=Decimal("5100"))

#: Sterling: a pre-tax rate with no tax stated on the page at all.
BARE_OLD = side(exclusive=Decimal("7000"), stored=Decimal("7000"))
BARE_NEW = side(exclusive=Decimal("7400"), stored=Decimal("7400"))

#: A change confirmed before the components were recorded. Nothing but the
#: number the alert has always quoted.
LEGACY_OLD = side(stored=Decimal("2500"))
LEGACY_NEW = side(stored=Decimal("2300"))


class TestASiteThatStatesItsTaxSeparately:
    def test_off_the_switch_it_is_the_room_alone(self):
        move = displayed_move(SPLIT_OLD, SPLIT_NEW, False)
        assert (move.old, move.new) == (Decimal("9000"), Decimal("9500"))
        assert move.note is None

    def test_on_the_switch_both_sides_are_the_sites_own_two_figures_added(self):
        move = displayed_move(SPLIT_OLD, SPLIT_NEW, True)
        assert (move.old, move.new) == (Decimal("10620"), Decimal("11210"))
        assert move.note is None

    def test_the_difference_is_recomputed_so_the_line_adds_up(self):
        """The whole reason the delta is not carried from the row.

        500 pre-tax is 590 all-in, and it is 590 that belongs next to 10,620
        and 11,210. Carrying the stored delta would print the pre-tax move
        under two all-in prices.
        """
        assert displayed_move(SPLIT_OLD, SPLIT_NEW, False).delta == Decimal("500.00")
        assert displayed_move(SPLIT_OLD, SPLIT_NEW, True).delta == Decimal("590.00")

    def test_the_percentage_survives_a_uniform_tax_rate_unchanged(self):
        """Both sides taxed at 18%, so the proportion is the same either way.

        Worth asserting rather than assuming: it is the case that makes the
        recomputation look pointless, and the next one is why it is not.
        """
        assert displayed_move(SPLIT_OLD, SPLIT_NEW, False).delta_pct == Decimal("5.56")
        assert displayed_move(SPLIT_OLD, SPLIT_NEW, True).delta_pct == Decimal("5.56")

    def test_the_percentage_moves_when_the_two_taxes_do_not_match(self):
        """Which is not hypothetical.

        Booking.com reported 375 tax on a 7,500 room and 1,620 on a 9,000 room
        at the same property on the same night -- 5.0% and 18.0%. Crossing that
        boundary, a 20.00% pre-tax rise is a 34.86% rise to the guest, and the
        stored percentage describes neither pair of numbers on the screen.
        """
        old = side(exclusive=Decimal("7500"), taxes=Decimal("375"), stored=Decimal("7500"))
        new = side(exclusive=Decimal("9000"), taxes=Decimal("1620"), stored=Decimal("9000"))

        assert displayed_move(old, new, False).delta_pct == Decimal("20.00")
        assert displayed_move(old, new, True).delta_pct == Decimal("34.86")
        assert displayed_move(old, new, True).delta == Decimal("2745.00")


class TestASiteThatPublishesOnlyOneOfTheTwo:
    def test_an_all_in_rate_asked_for_pre_tax_is_shown_and_marked(self):
        move = displayed_move(ALL_IN_OLD, ALL_IN_NEW, False)
        assert (move.old, move.new) == (Decimal("4822"), Decimal("5100"))
        assert move.note == INCLUSIVE_NOTE

    def test_an_all_in_rate_asked_for_all_in_needs_no_mark(self):
        assert displayed_move(ALL_IN_OLD, ALL_IN_NEW, True).note is None

    def test_a_rate_with_no_tax_published_is_never_grossed_up(self):
        """Sterling. 12% and 18% are both plausible and neither is knowable.

        The number stays exactly as the site printed it, and the message says
        which basis that is. See the module note in services/price_display.
        """
        move = displayed_move(BARE_OLD, BARE_NEW, True)
        assert (move.old, move.new) == (Decimal("7000"), Decimal("7400"))
        assert move.note == EXCLUSIVE_NOTE


class TestASideWithNoComponentsAtAll:
    def test_the_price_the_alert_has_always_quoted_is_still_quoted(self):
        move = displayed_move(LEGACY_OLD, LEGACY_NEW, True)
        assert (move.old, move.new) == (Decimal("2500"), Decimal("2300"))

    def test_it_is_marked_when_tax_was_asked_for(self):
        assert displayed_move(LEGACY_OLD, LEGACY_NEW, True).note == EXCLUSIVE_NOTE

    def test_and_not_when_it_was_not(self):
        assert displayed_move(LEGACY_OLD, LEGACY_NEW, False).note is None


class TestTwoSidesThatCannotBePutOnOneBasis:
    """A site that starts publishing its tax between one baseline and the next.

    Rare, and exactly the case a reader must not be left to work out: the
    difference between a pre-tax "was" and an all-in "now" is mostly tax, and
    it would be printed as though the hotel had moved its rate.
    """

    def test_the_pair_says_so(self):
        move = displayed_move(BARE_OLD, SPLIT_NEW, True)
        assert move.note == MIXED_NOTE

    def test_a_pair_marked_the_same_way_says_it_once(self):
        move = displayed_move(ALL_IN_OLD, ALL_IN_NEW, False)
        assert move.note == INCLUSIVE_NOTE

    def test_neither_side_is_grossed_up_when_only_one_side_can_be(self):
        """The stored pair, not one pre-tax figure beside one all-in one.

        Marking is not enough: the number beside the mark is the one people
        act on. Grossing up only the side that can be turns Sterling's real
        five percent drop into a third of a percent.
        """
        move = displayed_move(BARE_OLD, SPLIT_NEW, True)
        assert (move.old, move.new) == (Decimal("7000"), Decimal("9500"))

    def test_the_difference_is_arithmetic_on_one_basis(self):
        move = displayed_move(BARE_OLD, SPLIT_NEW, True)
        assert move.delta == Decimal("2500.00")
        assert move.new - move.old == move.delta

    def test_the_real_move_survives_a_baseline_written_before_the_components(self):
        """Sterling as this database actually held it, mid-migration.

        A baseline with no components and a new reading with them. The stored
        pair is a 5.06% drop; grossing up only the new side reports 0.32%.
        """
        move = displayed_move(
            side(stored=Decimal("3228.57")),
            side(
                exclusive=Decimal("3065.10"),
                taxes=Decimal("153.26"),
                stored=Decimal("3065.10"),
            ),
            True,
        )
        assert move.note == MIXED_NOTE
        assert (move.old, move.new) == (Decimal("3228.57"), Decimal("3065.10"))
        assert move.delta == Decimal("-163.47")
        assert move.delta_pct == Decimal("-5.06")

    def test_a_pair_that_can_both_be_grossed_up_still_is(self):
        """The stored pair is the last resort, never a preference."""
        move = displayed_move(SPLIT_OLD, SPLIT_NEW, True)
        assert move.note is None
        assert (move.old, move.new) == (Decimal("10620"), Decimal("11210"))

    def test_a_sell_out_against_a_mixed_side_keeps_its_empty_side(self):
        """One side has no price, so there is no pair to revert to."""
        move = displayed_move(BARE_OLD, side(), True)
        assert move.new is None
        assert move.delta is None


class TestARoomWithOnlyOneSide:
    """Sold out, or back on sale. There is no difference to state."""

    def test_a_sell_out_has_no_difference_and_no_percentage(self):
        move = displayed_move(SPLIT_OLD, side(), True)
        assert move.old == Decimal("10620")
        assert move.new is None
        assert move.delta is None and move.delta_pct is None

    def test_the_empty_side_does_not_make_the_pair_mixed(self):
        """An absent price has no basis to disagree about.

        Letting it vote would brand every sell-out "mixed tax basis", which is
        a warning about nothing on the one line that is already unusual.
        """
        assert displayed_move(SPLIT_OLD, side(), True).note is None

    def test_a_marked_survivor_still_carries_its_mark(self):
        assert displayed_move(ALL_IN_OLD, side(), False).note == INCLUSIVE_NOTE


# -- what the message actually says -----------------------------------
def line(**kwargs) -> ChangeLine:
    base = dict(
        hotel_name="A R Thanga Kottai",
        room_name="Deluxe Pool View",
        old_price=Decimal("10620"),
        new_price=Decimal("11210"),
        delta=Decimal("590"),
        delta_pct=Decimal("5.55"),
        currency="INR",
        direction="increase",
        check_in="2026-12-20",
        check_out="2026-12-21",
    )
    base.update(kwargs)
    return ChangeLine(**base)


class TestTheMessageSaysWhichBasisItIsOn:
    """Said once, on the stamp every channel already carries.

    A separate footer would need a new WhatsApp template variable, and a new
    template needs Meta's approval. This needs neither and cannot drift
    between the email and the WhatsApp.
    """

    def test_a_message_with_tax_says_so(self):
        message = render_digest("A R Thanga Kottai", [line()], with_tax=True)
        assert "prices incl. tax" in message.text
        assert "prices incl. tax" in message.html
        assert "prices incl. tax" in message.template_params[-1]

    def test_a_message_without_tax_says_that_too(self):
        """Silence was the bug. Off is a claim, not the absence of one."""
        message = render_digest("A R Thanga Kottai", [line()], with_tax=False)
        assert "prices excl. tax" in message.text

    def test_the_summary_states_it_the_same_way(self):
        message = render_summary([line()], window_hours=2, with_tax=True)
        assert "prices incl. tax" in message.text
        assert "prices incl. tax" in message.template_params[-1]


class TestARoomThatDisagreesWithTheRestOfTheMessage:
    def test_it_carries_its_own_marker(self):
        message = render_digest(
            "TREEBO MIDVALLEY",
            [line(basis_note=INCLUSIVE_NOTE)],
            with_tax=False,
        )
        assert INCLUSIVE_NOTE in message.text
        assert INCLUSIVE_NOTE in message.html

    def test_the_ordinary_room_carries_none(self):
        message = render_digest("A R Thanga Kottai", [line()], with_tax=True)
        assert EXCLUSIVE_NOTE not in message.text
        assert INCLUSIVE_NOTE not in message.text.replace("prices incl. tax", "")

    def test_whatsapp_marks_the_figure_the_reader_acts_on(self):
        """The template allows no line breaks, so the mark goes on one price.

        The "now" figure, because that is the one a decision is made from --
        and the "was" figure only when there is no "now" at all.
        """
        params = render_digest(
            "TREEBO MIDVALLEY", [line(basis_note=INCLUSIVE_NOTE)], with_tax=False
        ).template_params
        assert INCLUSIVE_NOTE in params[3]
        assert INCLUSIVE_NOTE not in params[2]

    def test_a_sold_out_room_marks_the_price_it_still_has(self):
        params = render_digest(
            "TREEBO MIDVALLEY",
            [
                line(
                    direction="became_unavailable",
                    new_price=None,
                    delta=None,
                    delta_pct=None,
                    basis_note=INCLUSIVE_NOTE,
                )
            ],
            with_tax=False,
        ).template_params
        assert INCLUSIVE_NOTE in params[2]

    def test_the_template_still_has_exactly_seven_variables(self):
        """Meta answers a wrong count with 132000, classified permanent.

        A marked line must not become an undeliverable one.
        """
        from app.notifications.base import WHATSAPP_TEMPLATE_PARAM_COUNT

        params = render_digest(
            "TREEBO MIDVALLEY", [line(basis_note=MIXED_NOTE)], with_tax=True
        ).template_params
        assert len(params) == WHATSAPP_TEMPLATE_PARAM_COUNT


# -- the join from a stored change to a rendered line ------------------
class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _Session:
    """Answers the two queries ``_render_lines`` makes, in order."""

    def __init__(self, *results):
        self._results = list(results)

    def execute(self, _statement):
        return _Result(self._results.pop(0))


def _stored_change(**kwargs):
    base = dict(
        id=1,
        offer_key="k" * 12,
        hotel_id=7,
        old_price=Decimal("9000"),
        new_price=Decimal("9500"),
        delta=Decimal("500"),
        delta_pct=Decimal("5.56"),
        currency="INR",
        direction="increase",
        previous_offer_key=None,
        old_price_exclusive=Decimal("9000"),
        old_taxes_fees=Decimal("1620"),
        old_price_inclusive=None,
        new_price_exclusive=Decimal("9500"),
        new_taxes_fees=Decimal("1710"),
        new_price_inclusive=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _lines_for(change, with_tax):
    series = SimpleNamespace(
        offer_key=change.offer_key,
        room_type_id=3,
        check_in=SimpleNamespace(isoformat=lambda: "2026-12-20"),
        check_out=SimpleNamespace(isoformat=lambda: "2026-12-21"),
        meal_plan="EP",
    )
    room = SimpleNamespace(id=3, name="Deluxe Pool View")
    hotels = {7: SimpleNamespace(id=7, name="A R Thanga Kottai")}
    session = _Session([series], [room])
    return _render_lines(session, [change], hotels, with_tax)[change.id]


class TestTheStoredChangeReachesTheLine:
    """``price_changes`` carries the components; the line carries the choice."""

    def test_off_the_switch_it_is_the_stored_pre_tax_pair(self):
        rendered = _lines_for(_stored_change(), with_tax=False)
        assert (rendered.old_price, rendered.new_price) == (
            Decimal("9000"),
            Decimal("9500"),
        )
        assert rendered.basis_note is None

    def test_on_the_switch_it_is_the_totals_and_the_bigger_move(self):
        rendered = _lines_for(_stored_change(), with_tax=True)
        assert (rendered.old_price, rendered.new_price) == (
            Decimal("10620"),
            Decimal("11210"),
        )
        assert rendered.delta == Decimal("590.00")

    def test_a_change_written_before_the_components_existed_still_sends(self):
        """Nothing is backfilled and nothing is guessed.

        The row quotes the only number it has, marked with the basis that
        number is on -- which is what the alert quoted yesterday.
        """
        rendered = _lines_for(
            _stored_change(
                old_price_exclusive=None,
                old_taxes_fees=None,
                new_price_exclusive=None,
                new_taxes_fees=None,
            ),
            with_tax=True,
        )
        assert (rendered.old_price, rendered.new_price) == (
            Decimal("9000"),
            Decimal("9500"),
        )
        assert rendered.basis_note == EXCLUSIVE_NOTE
