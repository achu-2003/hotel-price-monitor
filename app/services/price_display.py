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

THE ALERTS READ THE SAME RULE
============================
A WhatsApp is a screen too, and for most of the people on the recipient list
it is the ONLY one they look at. It used to quote ``price_changes.old_price``
and ``new_price`` verbatim -- both on the comparison basis, neither labelled --
so a manager who set the matrix to all-in rates got a message contradicting it
about the same room. ``displayed_move`` puts both sides of a move through the
fallback above and recomputes the difference from the two figures that will
actually be printed, because three numbers that do not add up cost more
credibility than either basis was worth.

PURE
====
No database, no clock, no settings lookup -- the series row and the flag are
both arguments. That is what lets the whole matrix of cases be tested without
a page, and it is why the callers pass ``show_with_tax`` down rather than
reading it here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
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


#: Shown against a MOVE whose two sides could not be put on the same basis.
#:
#: One number pre-tax and the other all-in makes the difference between them
#: arithmetic on two different things -- a 12% "increase" that is the tax
#: appearing, not the hotel moving. It happens when a site starts or stops
#: publishing its tax between one confirmed price and the next, which is rare
#: and is exactly the case a reader must not be left to work out.
MIXED_NOTE = "mixed tax basis"

#: Money is compared and reported to the paisa; percentages to two places.
#: The same quantisation ``services/comparison.py`` applies when it writes a
#: change, so a move recomputed on the display basis rounds the way the stored
#: one did rather than a hair differently.
_CENTS = Decimal("0.01")
_PCT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Components:
    """What one side of a price was made of, in the shape :func:`displayed_price` reads.

    A plain carrier so a ``price_changes`` row -- which holds two sides of a
    move and no series at all -- can go through the same fallback as a series
    row on a screen. One rule, one place; the alternative is a second copy of
    it in the renderer, which is how the dashboard and the alert came to
    disagree about what a price is in the first place.
    """

    current_price: Decimal | None = None
    last_price_exclusive: Decimal | None = None
    last_taxes_fees: Decimal | None = None
    last_price_inclusive: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ShownMove:
    """A confirmed price change, re-read on the basis the switch asked for."""

    old: Decimal | None
    new: Decimal | None
    delta: Decimal | None
    delta_pct: Decimal | None
    note: str | None = None


def displayed_move(
    old: Components, new: Components, show_with_tax: bool
) -> ShownMove:
    """Both sides of a move, and the difference between the two AS SHOWN.

    THE DELTA IS RECOMPUTED, NOT CARRIED
    ====================================
    The stored ``delta`` belongs to the stored pair, which are on the
    comparison basis. Print all-in prices above a pre-tax difference and the
    three numbers on the line do not add up:

        9,000 -> 9,500 (Increase 500)   became
        10,620 -> 11,210 (Increase 500) with the switch on, where it is 590

    A reader who checks the arithmetic of one alert and finds it wrong stops
    checking the others, so the difference is derived from the two numbers
    actually printed beside it.

    Where a side has no price -- a room that sold out, or one coming back --
    there is no difference to state and both figures are None. That is the
    same rule ``compare`` applies, and it is why "sold out" never reads as a
    drop to zero.

    THE NOTE IS ABOUT THE PAIR
    ==========================
    A side that could not be put on the asked-for basis is marked, exactly as
    a cell is on the matrix. When both are marked the same way the mark is
    said once. When they are marked DIFFERENTLY the pair is incomparable and
    says so: see :data:`MIXED_NOTE`.

    A MIXED PAIR IS NOT SHOWN ON THE ASKED-FOR BASIS AT ALL
    =======================================================
    Marking an incomparable pair is not enough, because the number beside the
    mark is the one people act on. Sterling, mid-migration: a baseline written
    before the components existed and a new reading written after.

        stored     3,228.57 -> 3,065.10   (-163.47, -5.06%)   the real move
        grossed    3,228.57 -> 3,218.36   ( -10.21, -0.32%)   pre-tax vs all-in

    A five percent drop reported as a third of a percent, because the
    difference is arithmetic on two different things -- the tax appearing, not
    the hotel moving. So when the two sides disagree the switch is not honoured
    for this line: both prices revert to the stored pair, which is one basis by
    construction, and the mark says the basis is not the one that was asked
    for. An honest pre-tax pair beats a mixed pair that reads as all-in.

    The stored pair is the last resort, not a preference. It is used only when
    the asked-for basis is unreachable for one side and reachable for the
    other; where both sides can be put on it, they are.
    """
    shown_old = displayed_price(old, show_with_tax)
    shown_new = displayed_price(new, show_with_tax)

    # Only the sides that carry a number can qualify one. A sold-out room's
    # empty side has no basis to disagree about, and letting it vote would
    # brand every sell-out "mixed".
    notes = {
        s.note
        for s in (shown_old, shown_new)
        if s.amount is not None and s.note is not None
    }
    priced = [s for s in (shown_old, shown_new) if s.amount is not None]
    if not notes:
        note = None
    elif len(notes) == 1 and (len(priced) == 1 or shown_old.note == shown_new.note):
        note = notes.pop()
    else:
        note = MIXED_NOTE

    amount_old, amount_new = shown_old.amount, shown_new.amount
    if note == MIXED_NOTE and old.current_price is not None and new.current_price is not None:
        # Both on the comparison basis, so the difference below is arithmetic
        # on one thing. See the docstring: the mark stays, the numbers revert.
        amount_old, amount_new = old.current_price, new.current_price

    delta = pct = None
    if amount_old is not None and amount_new is not None:
        delta = (amount_new - amount_old).quantize(_CENTS, rounding=ROUND_HALF_UP)
        pct = _percent(amount_old, amount_new)

    return ShownMove(
        old=amount_old, new=amount_new, delta=delta, delta_pct=pct, note=note
    )


def _percent(old: Decimal, new: Decimal) -> Decimal:
    """Percentage change, guarding a zero baseline the way ``comparison`` does."""
    if old == 0:
        return Decimal("100.00") if new != 0 else Decimal("0.00")
    return ((new - old) / old * 100).quantize(_PCT, rounding=ROUND_HALF_UP)


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
