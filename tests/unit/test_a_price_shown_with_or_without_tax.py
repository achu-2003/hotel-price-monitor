"""One switch decides whether a displayed price carries tax.

WHAT THIS IS FOR
================
The ten hotels do not agree on what a price is. Six sites quote the room
before tax and state the tax beside it; Sterling quotes before tax and states
no tax anywhere; the three Treebo properties publish one all-in figure and no
pre-tax number at all.

Every series is stored on the configured comparison basis, so all ten reached
the matrix as one column of numbers. On 5 Sep that column read:

    A R Thanga Kottai · Deluxe Pool view   9,995   <- before 1,836 tax
    TREEBO MIDVALLEY  · Deluxe Room        4,822   <- tax already inside

Both rendered identically, and nothing on the page said which was which.

THE RULE
========
Show the component that was asked for. Where the site did not publish it, show
the one it did and MARK it -- a room with a price is not a room without one,
and blanking it would hide a real number behind a preference.

WHAT IS NEVER DONE
==================
No tax rate is inferred. Indian hotel GST is 12% under 7,500 a night and 18%
at or above, so grossing up a pre-tax figure looks trivial. The data refuses
it: Booking.com reported 375 tax on a 7,500 room (5.0%) and 1,620 on a 9,000
room (18.0%) at the same property on the same night. Adding two numbers a site
printed is reporting; deriving a third is guessing.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.price_display import (
    EXCLUSIVE_NOTE,
    INCLUSIVE_NOTE,
    Shown,
    cheapest,
    displayed_price,
    displayed_price_sql,
    is_on_asked_basis_sql,
)


def series(exclusive=None, taxes=None, inclusive=None, current=None):
    return SimpleNamespace(
        last_price_exclusive=exclusive,
        last_taxes_fees=taxes,
        last_price_inclusive=inclusive,
        current_price=current,
    )


#: Booking.com and Cleartrip: the room, and the tax, stated separately.
SPLIT = series(exclusive=Decimal("9000"), taxes=Decimal("1620"), current=Decimal("9000"))
#: Treebo: one all-in figure, no pre-tax number published anywhere.
ALL_IN = series(inclusive=Decimal("4822"), current=Decimal("4822"))
#: Sterling: a pre-tax rate, and the page states no tax at all.
BARE = series(exclusive=Decimal("6000"), current=Decimal("6000"))


class TestTheSwitchOff:
    """The pre-tax number — what this deployment has always shown."""

    def test_a_site_that_states_the_tax_shows_the_room_alone(self):
        assert displayed_price(SPLIT, False) == Shown(Decimal("9000"))

    def test_an_all_in_price_is_shown_and_marked(self):
        """Treebo publishes no pre-tax figure, so there is nothing to strip.

        Marked rather than converted: dividing by an assumed GST rate would
        produce a number the hotel never quoted, presented with the same
        confidence as the six beside it that are real.
        """
        shown = displayed_price(ALL_IN, False)
        assert shown.amount == Decimal("4822")
        assert shown.note == INCLUSIVE_NOTE
        assert shown.is_qualified

    def test_a_bare_pre_tax_rate_needs_no_marker(self):
        assert displayed_price(BARE, False) == Shown(Decimal("6000"))


class TestTheSwitchOn:
    """The number the guest actually pays, where the site says what that is."""

    def test_the_room_and_its_tax_are_added(self):
        assert displayed_price(SPLIT, True) == Shown(Decimal("10620"))

    def test_a_published_all_in_price_is_used_as_it_stands(self):
        """No marker: this IS the tax-inclusive figure that was asked for."""
        assert displayed_price(ALL_IN, True) == Shown(Decimal("4822"))

    def test_a_rate_with_no_tax_published_is_shown_and_marked(self):
        shown = displayed_price(BARE, True)
        assert shown.amount == Decimal("6000")
        assert shown.note == EXCLUSIVE_NOTE

    def test_a_tax_of_none_is_not_a_tax_of_zero(self):
        """NULL means the site did not say, and it must not read as free.

        Treating a missing tax as 0 would total Sterling at exactly its
        pre-tax rate and show it unmarked — a number indistinguishable from a
        site that had genuinely quoted an all-in price.
        """
        assert displayed_price(BARE, True).note == EXCLUSIVE_NOTE
        assert displayed_price(series(exclusive=Decimal("6000"), taxes=Decimal("0")), True) == Shown(
            Decimal("6000")
        )


class TestARowWithNoComponents:
    """Written before this shipped, or by a source with no breakdown at all."""

    def test_the_price_already_on_the_screen_is_kept(self):
        assert displayed_price(series(current=Decimal("1234")), False).amount == Decimal("1234")
        assert displayed_price(series(current=Decimal("1234")), True).amount == Decimal("1234")

    def test_it_is_marked_when_tax_was_asked_for(self):
        """It is a comparison-basis number, which on this deployment is pre-tax."""
        assert displayed_price(series(current=Decimal("1234")), True).note == EXCLUSIVE_NOTE

    def test_a_sold_out_room_with_no_price_stays_empty(self):
        assert displayed_price(series(), True) == Shown(None)
        assert displayed_price(series(), False) == Shown(None)


class TestTheCheapestOfARow:
    def test_it_is_taken_from_the_rendered_numbers(self):
        """Not re-read off the series.

        Totalling the cells with tax while the summary beside them stayed
        pre-tax would put a "cheapest" on the row lower than every price in it.
        """
        assert cheapest([Shown(Decimal("10620")), Shown(Decimal("4822"))]) == Decimal("4822")

    def test_a_row_with_nothing_priced_has_no_cheapest(self):
        assert cheapest([Shown(None), Shown(None)]) is None
        assert cheapest([]) is None


class TestTheSqlFormAgreesWithThePythonForm:
    """The overview aggregates with MIN(), so the rule exists twice.

    Two copies of a rule are two copies until something proves they agree.
    This compiles the SQL and checks the fallback ORDER matches the Python
    branch order — the part that would silently diverge, because both forms
    return a plausible number either way.
    """

    def test_with_tax_prefers_published_inclusive_then_the_sum(self):
        sql = str(displayed_price_sql(True))
        assert sql.index("last_price_inclusive") < sql.index("last_price_exclusive")
        assert "last_price_exclusive + price_series.last_taxes_fees" in sql

    def test_without_tax_prefers_exclusive(self):
        sql = str(displayed_price_sql(False))
        assert sql.index("last_price_exclusive") < sql.index("last_price_inclusive")

    def test_current_price_is_the_last_resort_either_way(self):
        for flag in (True, False):
            sql = str(displayed_price_sql(flag))
            assert sql.rindex("current_price") > sql.index("last_price_")

    def test_both_forms_fall_back_in_the_same_order(self):
        """The COALESCE arguments, in order, are the Python branches in order.

        A NULL component drops out of COALESCE exactly as it fails the
        ``is not None`` test above it, and the sum goes NULL when either half
        is missing — which is the same condition as "both present".
        """
        assert str(displayed_price_sql(True)).count("coalesce") == 1
        assert str(displayed_price_sql(False)).count("coalesce") == 1


class TestTheOverviewOnlyClaimsWhatItCanShow:
    """The per-hotel "from" figure is aggregated, so its label has to be too.

    It used to append "incl. tax" whenever the switch was on. That is a claim
    about a NUMBER, not about a setting, and on a hotel whose components are
    not recorded the number is the pre-tax one -- so the label said the
    opposite of the truth on exactly the rows that most needed it. A R Thanga
    Kottai read "from 8,995 incl. tax" about 8,995 before 1,652 of tax.
    """

    def test_with_tax_accepts_a_published_total_or_a_stated_tax(self):
        sql = str(is_on_asked_basis_sql(True))
        assert "last_price_inclusive IS NOT NULL" in sql
        assert "last_taxes_fees IS NOT NULL" in sql

    def test_without_tax_asks_only_for_the_pre_tax_figure(self):
        sql = str(is_on_asked_basis_sql(False))
        assert "last_price_exclusive IS NOT NULL" in sql
        assert "last_taxes_fees" not in sql

    def test_a_row_with_no_components_satisfies_neither(self):
        """The fallback to current_price is a price, not a basis.

        displayed_price still shows it -- a number on the screen beats a blank
        -- and marks it. The label on an aggregate has no such escape, so the
        condition must exclude it rather than wave it through.
        """
        for flag in (True, False):
            sql = str(is_on_asked_basis_sql(flag))
            assert "current_price" not in sql
