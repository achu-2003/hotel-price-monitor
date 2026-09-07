"""Your rate against everybody else's, one room category at a time.

WHAT THIS ANSWERS THAT THE MATRIX DOES NOT
==========================================
The matrix prints every room of every property side by side. It is the right
raw view and the wrong shape for the one question that actually gets asked
every morning: *we are asking 5,500 for a Classic Room — where does that put
us?* Reading that off the matrix means finding your own row, remembering the
figure, then scanning nine other rows for whichever of their rooms is the
same tier, and doing the subtraction in your head. Ten properties and seven
categories is seventy subtractions, and a subtraction done in the head is a
subtraction nobody does twice.

So this page does them. One column per room category, one row per competitor,
and in every cell the gap between their cheapest room of that tier and yours.
It is the sheet the team already kept by hand — resorts down the side, room
categories across the top — with the prices filled in.

THE BASELINE IS A PROPERTY, NOT AN AVERAGE
==========================================
The comparison is against your own property (``hotels.is_own_property``),
never against the set's mean. An average moves when a competitor is full,
which would make your position appear to change on a morning when your rate
and theirs did not — and it answers a question about the market rather than
the question being asked, which is about you.

A property that is not yours cannot be the baseline. That is deliberate: the
number this page exists to produce is "how far above or below US", and a page
that would silently benchmark against a competitor's rate has produced a
number that reads the same and means something else.

WHAT IS COMPARED IS THE CHEAPEST ROOM ON SALE IN THE TIER
=========================================================
A property can sell three classic rooms (Sterling sells "Classic Room",
"Classic Room with Balcony" and "Mountain View Classic Room"). The entry
price for the tier is the cheapest of them that a guest can actually book, so
that is the figure — the same rule the matrix's Cheapest column follows, for
the same reason: it is the number a rate decision is made against.

Sold-out rooms are excluded from that minimum and never carry a gap. A rate
nobody can book is not a rate you are competing with, and letting a full
room's last-known price into the arithmetic would report a gap against a
price that has stopped existing. Where a hotel sells the tier and every room
in it is full, the cell says so: being full is intelligence in its own right,
and it is a different fact from not selling the tier at all.

TWO PRICES ON DIFFERENT BASES DO NOT SUBTRACT
=============================================
``services/price_display`` marks a cell whose site published tax and not the
pre-tax figure, or the reverse, because a pre-tax rate beside an all-in one
reads as a competitor undercutting by 15% when they are not. A gap between
two such numbers is that same error with the evidence removed — the marker
is on the two prices, and the difference between them arrives with no marker
at all. So a gap computed across bases is flagged (:attr:`Cell.mixed_basis`)
and the page qualifies it. It is not suppressed: a gap that is roughly right
and says so beats a blank cell, and the tax component is a known fraction
rather than an unknown.

Currencies are the harder case and are simply not subtracted. Two properties
quoting different currencies have no gap here at all, because converting them
would need a rate this system does not hold and does not guess at.

"OTHER" IS A COLUMN WITH NO GAPS IN IT
======================================
``room_category.OTHER`` is not a tier. It is where a name that states no tier
lands, so two rooms in it have nothing in common except that neither could be
placed — and subtracting a dorm bed from a treehouse produces a number with
the same look as a real one. The column is still shown, because a room that
vanished from the page would be a room nobody knows is missing, but nothing
in it is subtracted.

PURE
====
No database and no clock: rows in, grid out. The same
``(series, hotel, room_name)`` tuples the matrix query already produces, so
the page costs one query it was going to run anyway.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.price_display import displayed_price
from app.services.room_category import CATEGORIES, OTHER, classify


@dataclass(frozen=True, slots=True)
class Column:
    """One room category, as a column of the grid."""

    slug: str
    label: str


@dataclass(frozen=True, slots=True)
class Cell:
    """One hotel's position in one category, ready to render.

    Four distinguishable states, and the template says a different thing for
    each because they call for different responses:

    ``price`` set
        the tier is on sale at this figure.
    ``sold_out``
        the hotel sells the tier and every room in it is full tonight.
    neither
        the hotel does not sell the tier at all (an empty cell, exactly as
        the hand-kept sheet leaves it).
    """

    price: Decimal | None = None
    currency: str = "INR"
    #: The room the figure belongs to. A gap is meaningless without knowing
    #: which of their rooms produced it.
    room_name: str | None = None
    #: Further rooms this hotel sells in the tier, above the cheapest one.
    #: Shown as "+2 more" rather than listed: the matrix lists them.
    also: int = 0
    #: The tax-basis marker from price_display, where the site published one
    #: component and not the other.
    note: str | None = None
    sold_out: bool = False

    #: Their price minus yours. None where either side has no bookable price,
    #: where the two are in different currencies, or in the Other column.
    gap: Decimal | None = None
    #: The same as a percentage of YOUR rate, so "+18%" means "18% above what
    #: we are asking" and not 18% of their own price.
    gap_pct: float | None = None
    #: Whether exactly one of the two prices had to fall back to the other tax
    #: basis, which makes the gap approximate. See the module docstring.
    mixed_basis: bool = False

    @property
    def has_price(self) -> bool:
        return self.price is not None

    @property
    def dearer(self) -> bool:
        """They are asking more than us."""
        return self.gap is not None and self.gap > 0

    @property
    def cheaper(self) -> bool:
        """They are undercutting us."""
        return self.gap is not None and self.gap < 0


@dataclass(frozen=True, slots=True)
class Row:
    """One property across every column, plus how it sits overall."""

    hotel: object
    cells: tuple[Cell, ...]
    is_baseline: bool = False

    @property
    def compared(self) -> int:
        """Categories where this row has a gap against the baseline."""
        return sum(1 for c in self.cells if c.gap is not None)

    @property
    def cheaper_in(self) -> int:
        return sum(1 for c in self.cells if c.cheaper)

    @property
    def dearer_in(self) -> int:
        return sum(1 for c in self.cells if c.dearer)


@dataclass(frozen=True, slots=True)
class Grid:
    """The whole page: a header row of categories, your row, and theirs."""

    columns: tuple[Column, ...]
    baseline: Row | None
    rivals: tuple[Row, ...]

    @property
    def is_empty(self) -> bool:
        return not self.columns


@dataclass(frozen=True, slots=True)
class _Priced:
    """One room of one hotel, after the tax switch has been applied."""

    amount: Decimal | None
    note: str | None
    currency: str
    room_name: str
    is_available: bool


def _cell_for(priced: list[_Priced]) -> Cell:
    """The tier's entry price for one hotel, or the fact that it has none."""
    on_sale = [p for p in priced if p.is_available and p.amount is not None]
    if not on_sale:
        # Rooms in this tier exist; not one of them can be booked tonight.
        # Deliberately not the last-known rate of the cheapest: see the module
        # docstring on why a full room carries no gap.
        return Cell(sold_out=bool(priced))

    best = min(on_sale, key=lambda p: p.amount)
    return Cell(
        price=best.amount,
        currency=best.currency,
        room_name=best.room_name,
        also=len(on_sale) - 1,
        note=best.note,
    )


def _against(cell: Cell, base: Cell, slug: str) -> Cell:
    """``cell`` with its gap against ``base`` filled in, where one is possible.

    Every reason to refuse is a reason the subtraction would produce a number
    that looks exactly like a real one: no bookable price on either side, two
    different currencies, or a column whose members are not the same kind of
    room.
    """
    if slug == OTHER:
        return cell
    if cell.price is None or base.price is None:
        return cell
    if cell.currency != base.currency:
        return cell

    gap = cell.price - base.price
    return Cell(
        price=cell.price,
        currency=cell.currency,
        room_name=cell.room_name,
        also=cell.also,
        note=cell.note,
        sold_out=cell.sold_out,
        gap=gap,
        gap_pct=float(gap / base.price * 100) if base.price else None,
        # One side fell back to the other tax basis and the other did not, so
        # the difference spans the tax line. Both qualified, or neither, and
        # the two numbers are at least on the same footing.
        mixed_basis=(cell.note is not None) != (base.note is not None),
    )


def build(rows, *, baseline_hotel_id: int | None, show_with_tax: bool = False) -> Grid:
    """Build the comparison grid from the matrix's own price rows.

    Args:
        rows: ``(series, hotel, room_name)`` for one night and occupancy.
        baseline_hotel_id: your property. ``None`` — nothing marked as yours —
            still builds the grid, with prices and no gaps, because the rates
            are worth reading while somebody goes and ticks the box.
        show_with_tax: the deployment-wide display basis.

    Columns are the categories that anybody prices tonight, in the sheet's
    order. A category only your property sells is a column too: "nobody else
    on this hill sells a pool suite" is the answer to a pricing question, and
    a column that disappeared would have hidden it.
    """
    hotels: dict[int, object] = {}
    priced: dict[int, dict[str, list[_Priced]]] = {}

    for series, hotel, room_name in rows:
        slug = classify(room_name)
        shown = displayed_price(series, show_with_tax)
        hotels.setdefault(hotel.id, hotel)
        priced.setdefault(hotel.id, {}).setdefault(slug, []).append(
            _Priced(
                amount=shown.amount,
                note=shown.note,
                currency=series.currency,
                room_name=room_name,
                is_available=bool(series.is_available),
            )
        )

    sold_anywhere = {slug for tiers in priced.values() for slug in tiers}
    columns = tuple(
        Column(c.slug, c.label) for c in CATEGORIES if c.slug in sold_anywhere
    )
    if not columns:
        return Grid(columns=(), baseline=None, rivals=())

    cells: dict[int, tuple[Cell, ...]] = {
        hotel_id: tuple(_cell_for(tiers.get(c.slug, [])) for c in columns)
        for hotel_id, tiers in priced.items()
    }

    base_cells = cells.get(baseline_hotel_id)
    baseline = (
        Row(hotel=hotels[baseline_hotel_id], cells=base_cells, is_baseline=True)
        if base_cells is not None
        else None
    )

    rivals = []
    for hotel_id, row_cells in cells.items():
        if hotel_id == baseline_hotel_id:
            continue
        if base_cells is not None:
            row_cells = tuple(
                _against(cell, base_cells[i], columns[i].slug)
                for i, cell in enumerate(row_cells)
            )
        rivals.append(Row(hotel=hotels[hotel_id], cells=row_cells))

    rivals.sort(key=lambda r: r.hotel.name)
    return Grid(columns=columns, baseline=baseline, rivals=tuple(rivals))
