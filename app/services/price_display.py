"""Which number to put on the screen, and when to admit it is not the one asked for.

THE PROBLEM THIS SOLVES
=======================
Ten hotels, three different ideas of what a price is:

* six sites quote the room before tax and state the tax beside it
* one quotes before tax and states no tax at all (Sterling / Hotelzify)
* three publish a single all-in figure and no pre-tax number (Treebo)

Every series is stored on the configured comparison basis, so all ten reach
the screen as one column of numbers -- and on the matrix that read as Treebo
being 11-15% cheaper or dearer than it is, with nothing on the page saying
which cell was which.

THE RULE
========
The switch on Settings says which basis the reader wants. Where the site
published that component, it is shown. Where it did not, the component we DO
have is shown and marked, because a room that has a price is not a room with
no price and blanking it would hide a real number behind a preference.

**Nothing here computes a tax rate.** The temptation is obvious -- Indian
hotel GST is 12% under 7,500 a night and 18% at or above, so a pre-tax figure
could be grossed up. The data says not to: Booking.com reported 375 on a 7,500
room (5.0%) and 1,620 on a 9,000 room (18.0%) for the same property on the
same night, so whatever it publishes as tax is not a uniform liability. A
number derived from that would be presented with the same confidence as one
the hotel actually quoted, and be wrong. Adding two figures the site itself
printed is reporting; inferring a third is guessing, and this codebase does
not guess at prices.

PURE
====
No database, no clock, no settings lookup -- the series row and the flag are
both arguments. That is what lets the whole matrix of cases be tested without
a page, and it is why the callers pass ``show_with_tax`` down rather than
reading it here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func


class _HasPriceComponents(Protocol):
    """What this module needs off a series row, and nothing more."""

    current_price: Decimal | None
    last_price_exclusive: Decimal | None
    last_taxes_fees: Decimal | None
    last_price_inclusive: Decimal | None


#: Shown against a cell whose number is not on the basis that was asked for.
INCLUSIVE_NOTE = "incl. tax"
EXCLUSIVE_NOTE = "excl. tax"


@dataclass(frozen=True, slots=True)
class Shown:
    """One number, ready to render, and whether it needs qualifying.

    ``note`` is None on the common path -- the site published the component
    that was asked for, and a marker on every cell would be noise. It is set
    only where the cell disagrees with the rest of the column, which is
    exactly where a reader would otherwise be misled.
    """

    amount: Decimal | None
    note: str | None = None

    @property
    def is_qualified(self) -> bool:
        return self.note is not None


def displayed_price(series: _HasPriceComponents, show_with_tax: bool) -> Shown:
    """The price to show for one series row.

    Falls back in a fixed order, and says so when it has fallen back:

    with tax
        the published all-in figure; else pre-tax PLUS the published tax --
        both numbers the site printed, added, which is reporting rather than
        inference; else the pre-tax figure, marked ``excl. tax``.

    without tax
        the published pre-tax figure; else the all-in figure, marked
        ``incl. tax``.

    ``current_price`` is the last resort in both directions. A row written
    before this feature shipped, or by a source that publishes a bare number
    with no component breakdown at all, still has a price worth showing -- it
    is the number that was on the screen yesterday, and the switch should not
    blank it.
    """
    exclusive = series.last_price_exclusive
    inclusive = series.last_price_inclusive
    taxes = series.last_taxes_fees

    if show_with_tax:
        if inclusive is not None:
            return Shown(inclusive)
        if exclusive is not None and taxes is not None:
            return Shown(exclusive + taxes)
        if exclusive is not None:
            # Sterling's case: a pre-tax rate and no tax published anywhere on
            # the page. Marked rather than grossed up -- see the module note.
            return Shown(exclusive, EXCLUSIVE_NOTE)
        return Shown(series.current_price, EXCLUSIVE_NOTE if series.current_price is not None else None)

    if exclusive is not None:
        return Shown(exclusive)
    if inclusive is not None:
        # Treebo's case: an all-in rate and no pre-tax figure to strip back to.
        return Shown(inclusive, INCLUSIVE_NOTE)
    return Shown(series.current_price)


def cheapest(shown: list[Shown]) -> Decimal | None:
    """The lowest of what is actually on the row.

    Takes the rendered numbers rather than re-reading the series, so the
    "cheapest" cell can never disagree with the cells beneath it -- which is
    what would happen the moment one column was totalled with tax and the
    summary was not.
    """
    amounts = [s.amount for s in shown if s.amount is not None]
    return min(amounts) if amounts else None


def displayed_price_sql(show_with_tax: bool):
    """:func:`displayed_price` as a SQL expression over ``price_series``.

    The overview aggregates the cheapest room per hotel with ``MIN()``, which
    has to happen in the database -- so the fallback order exists twice, and
    two copies of a rule are two copies until something proves they agree.
    They live in one module for that reason, and
    ``test_a_price_shown_with_or_without_tax`` runs both over the same rows and
    asserts the answers match.

    No ``note`` here: an aggregate over many rooms has no single basis to
    qualify. The per-room cells carry the marker, and the summary is a "from"
    figure that points at them.
    """
    # Imported here rather than at module scope: this module is pure logic and
    # is imported by tests that have no database and no ORM registry mapped.
    from app.db.models import PriceSeries

    if show_with_tax:
        return func.coalesce(
            PriceSeries.last_price_inclusive,
            PriceSeries.last_price_exclusive + PriceSeries.last_taxes_fees,
            PriceSeries.last_price_exclusive,
            PriceSeries.current_price,
        )
    return func.coalesce(
        PriceSeries.last_price_exclusive,
        PriceSeries.last_price_inclusive,
        PriceSeries.current_price,
    )


def is_on_asked_basis_sql(show_with_tax: bool):
    """Whether a series row can honour the switch out of its own components.

    The overview prints one "from" figure per hotel, aggregated in SQL, and it
    used to append "incl. tax" whenever the switch was on. That is a claim
    about a number, not about a setting, and for a hotel whose components are
    not recorded the number is the pre-tax one -- so the label said the
    opposite of the truth on exactly the rows that needed it most. A R Thanga
    Kottai read "from 8,995 incl. tax" about 8,995 before 1,652 of tax.

    Paired with ``bool_and`` it answers "is every room behind this figure on
    the basis that was asked for", which is the only condition under which the
    row may say so.
    """
    from app.db.models import PriceSeries

    if show_with_tax:
        return (PriceSeries.last_price_inclusive.is_not(None)) | (
            PriceSeries.last_price_exclusive.is_not(None)
            & PriceSeries.last_taxes_fees.is_not(None)
        )
    return PriceSeries.last_price_exclusive.is_not(None)
