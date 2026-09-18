"""The history chart and the matrix agree about what a night costs.

Six of the ten sources quote a room before tax and print the tax beside it,
so their observations carry ``price_exclusive`` and ``taxes_fees`` and store
NO all-in figure. The matrix has always added those two published numbers
(services/price_display.py); the history endpoint returned the bare column.

The result was one offer shown two ways on the same screen: the table said
10,620 and the chart under it plotted null for every point, for A R Thanga
Kottai, Ananthyam, ASG and all three MGM properties -- the majority of the
portfolio. Both now read the same rule from the same function.
"""
from decimal import Decimal

from app.services.price_display import all_in_price


#: Booking.com and Cleartrip: the room, and the tax, stated separately.
SPLIT = (None, Decimal("9000"), Decimal("1620"))
#: Treebo: one all-in figure, no pre-tax number published anywhere.
ALL_IN = (Decimal("4822"), None, None)
#: Sterling / Hotelzify: a pre-tax rate, and no tax stated on the page.
BARE = (None, Decimal("6000"), None)


def test_a_room_and_its_stated_tax_are_added():
    assert all_in_price(*SPLIT) == Decimal("10620")


def test_a_published_all_in_price_is_used_as_it_stands():
    assert all_in_price(*ALL_IN) == Decimal("4822")


def test_a_rate_with_no_tax_published_yields_nothing():
    """None, not the pre-tax figure.

    The caller decides what to do with the gap: the matrix shows the pre-tax
    number MARKED ``excl. tax``, which a chart axis has no room to say. An
    unmarked 6,000 here would be indistinguishable from a genuine all-in quote.
    """
    assert all_in_price(*BARE) is None


def test_a_tax_of_zero_is_not_a_missing_tax():
    """A site that states 0 tax has told us the total; NULL has not."""
    assert all_in_price(None, Decimal("6000"), Decimal("0")) == Decimal("6000")


def test_nothing_published_is_not_a_price_of_zero():
    assert all_in_price(None, None, None) is None
    assert all_in_price(None, None, Decimal("1620")) is None


def test_the_matrix_and_the_chart_cannot_drift_apart():
    """The switch-on branch of displayed_price is this same function."""
    from types import SimpleNamespace

    from app.services.price_display import Shown, displayed_price

    inclusive, exclusive, taxes = SPLIT
    row = SimpleNamespace(
        last_price_exclusive=exclusive,
        last_taxes_fees=taxes,
        last_price_inclusive=inclusive,
        current_price=exclusive,
    )
    assert displayed_price(row, True) == Shown(all_in_price(*SPLIT))
