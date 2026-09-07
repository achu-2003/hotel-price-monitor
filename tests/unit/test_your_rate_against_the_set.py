"""The comparison grid: our rate, theirs, and the gap between the two.

The matrix prints every room of every property. This grid does the
subtraction, one room category at a time, against the property marked as ours.
A wrong number here is worse than a missing one -- it is a rate decision made
on arithmetic nobody re-checked -- so what is pinned below is mostly the cases
where the subtraction must NOT happen: a full room, a room in a currency we
did not quote, a column whose rooms are not the same kind of room.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services import rate_gap
from app.services.room_category import CLASSIC, DELUXE, OTHER, SUITE, VILLA_2BR

AGS = SimpleNamespace(id=1, name="AGS Holiday Resorts", is_own_property=True)
STERLING = SimpleNamespace(id=2, name="Sterling", is_own_property=False)
MGM = SimpleNamespace(id=3, name="MGM Whispering Winds", is_own_property=False)


def _series(price, *, available=True, currency="INR", basis=None):
    """One price row.

    ``basis`` says which component the site published, because that is what
    decides whether the number reaching the grid is on the basis asked for:
    ``None`` publishes neither and falls back to ``current_price`` (the
    unqualified path both switch positions take), ``"incl"`` publishes only
    the all-in figure, ``"excl"`` only the pre-tax one.
    """
    amount = Decimal(price) if price is not None else None
    return SimpleNamespace(
        offer_key=f"k{price}-{currency}",
        current_price=amount,
        last_price_exclusive=amount if basis == "excl" else None,
        last_taxes_fees=None,
        last_price_inclusive=amount if basis == "incl" else None,
        currency=currency,
        is_available=available,
    )


#: Three properties. AGS sells classic, deluxe and suite; Sterling undercuts on
#: classic and sits above on suite; MGM sells a category AGS does not.
ROWS = [
    (_series(5500), AGS, "Classic Room"),
    (_series(7200), AGS, "Deluxe Room"),
    (_series(11000), AGS, "Grandeur Suite"),
    (_series(4800), STERLING, "Classic Room"),
    (_series(6100), STERLING, "Classic Room with Balcony"),
    (_series(12500), STERLING, "Junior Suite"),
    (_series(5500), MGM, "Club Room"),
    (_series(19000), MGM, "Cottage - 2 Bed Room, Pool View Sitout"),
]


def _grid(rows=ROWS, baseline=AGS.id, show_with_tax=False):
    return rate_gap.build(
        rows, baseline_hotel_id=baseline, show_with_tax=show_with_tax
    )


def _cell(grid, hotel, slug):
    row = (
        grid.baseline
        if grid.baseline and grid.baseline.hotel.id == hotel.id
        else next(r for r in grid.rivals if r.hotel.id == hotel.id)
    )
    return row.cells[[c.slug for c in grid.columns].index(slug)]


class TestTheShapeOfTheSheet:
    def test_columns_are_the_categories_anyone_prices(self):
        assert [c.slug for c in _grid().columns] == [CLASSIC, DELUXE, SUITE, VILLA_2BR]

    def test_columns_keep_the_sheets_order(self):
        """Cheapest tier first, biggest unit last -- the order the hand-kept
        sheet used and the order a person scans left to right. Not the order
        the classifier happens to try its rules in."""
        labels = [c.label for c in _grid().columns]
        assert labels == ["Classic Room", "Deluxe", "Suite", "2 Bed Room Villa"]

    def test_a_category_nobody_sells_is_not_a_column(self):
        assert "pool-suite" not in [c.slug for c in _grid().columns]

    def test_our_property_is_the_baseline_row_and_not_a_rival(self):
        grid = _grid()
        assert grid.baseline.hotel is AGS
        assert grid.baseline.is_baseline
        assert AGS.id not in [r.hotel.id for r in grid.rivals]

    def test_rivals_are_in_name_order(self):
        assert [r.hotel.name for r in _grid().rivals] == [
            "MGM Whispering Winds", "Sterling"
        ]

    def test_no_rows_at_all_is_an_empty_grid(self):
        assert rate_gap.build([], baseline_hotel_id=AGS.id).is_empty


class TestTheGap:
    def test_a_competitor_under_us_is_a_negative_gap(self):
        cell = _cell(_grid(), STERLING, CLASSIC)
        assert cell.gap == Decimal(-700)
        assert cell.cheaper and not cell.dearer

    def test_a_competitor_over_us_is_a_positive_gap(self):
        cell = _cell(_grid(), STERLING, SUITE)
        assert cell.gap == Decimal(1500)
        assert cell.dearer and not cell.cheaper

    def test_the_percentage_is_of_our_rate_not_theirs(self):
        """"18% above us" has to mean 18% of what WE are asking. Taken over
        their own price the same gap reads as a different number, and the two
        agree closely enough that nobody would catch it."""
        cell = _cell(_grid(), STERLING, CLASSIC)
        assert round(cell.gap_pct, 2) == round(-700 / 5500 * 100, 2)

    def test_matching_the_market_exactly_is_a_gap_of_zero_not_a_blank(self):
        """MGM's Club Room is priced at exactly our classic rate. Zero is an
        answer -- 'level with us' -- and dropping it would leave a cell that
        reads identically to one we could not compare."""
        cell = _cell(_grid(), MGM, CLASSIC)
        assert cell.gap == 0
        assert not cell.cheaper and not cell.dearer

    def test_our_own_row_carries_no_gaps(self):
        assert all(c.gap is None for c in _grid().baseline.cells)

    def test_a_row_says_how_many_tiers_it_undercuts_us_on(self):
        row = next(r for r in _grid().rivals if r.hotel.id == STERLING.id)
        assert (row.compared, row.cheaper_in, row.dearer_in) == (2, 1, 1)


class TestWhatIsCompared:
    def test_the_cheapest_room_on_sale_in_the_tier(self):
        """Sterling sells two classic rooms. The tier's entry price is the
        cheaper one -- the same rule the matrix's Cheapest column follows."""
        cell = _cell(_grid(), STERLING, CLASSIC)
        assert cell.price == Decimal(4800)
        assert cell.room_name == "Classic Room"

    def test_the_other_rooms_of_the_tier_are_counted_not_lost(self):
        assert _cell(_grid(), STERLING, CLASSIC).also == 1

    def test_a_full_room_is_never_the_price_even_when_it_is_the_cheapest(self):
        """THE BUG THIS PREVENTS: a sold-out room's last known rate winning the
        minimum, and the whole column reporting a gap against a price the
        hotel has stopped charging."""
        rows = [
            (_series(5500), AGS, "Classic Room"),
            (_series(3900, available=False), STERLING, "Classic Room"),
            (_series(6100), STERLING, "Classic Room with Balcony"),
        ]
        cell = _cell(_grid(rows), STERLING, CLASSIC)
        assert cell.price == Decimal(6100)
        assert cell.gap == Decimal(600)

    def test_a_tier_that_is_entirely_full_says_so_and_carries_no_gap(self):
        rows = [
            (_series(5500), AGS, "Classic Room"),
            (_series(4800, available=False), STERLING, "Classic Room"),
        ]
        cell = _cell(_grid(rows), STERLING, CLASSIC)
        assert cell.sold_out
        assert cell.price is None and cell.gap is None

    def test_a_tier_the_property_does_not_sell_is_empty_not_sold_out(self):
        """Two different facts, and the template says a different thing for
        each. 'Sold out' about a category a property has never sold would be
        an invented reading of a hotel nobody asked about."""
        cell = _cell(_grid(), MGM, SUITE)
        assert not cell.sold_out
        assert cell.price is None and cell.gap is None

    def test_a_category_only_we_sell_is_still_a_column(self):
        """'Nobody else on this hill sells a deluxe' is the answer to a pricing
        question. A column that vanished would have hidden it."""
        assert _cell(_grid(), AGS, DELUXE).price == Decimal(7200)
        assert _cell(_grid(), STERLING, DELUXE).price is None


class TestWhatIsNotSubtracted:
    def test_two_currencies_do_not_subtract(self):
        """Converting them would need a rate this system does not hold. A gap
        of '+₹1,200' between an INR price and a USD one is a number with the
        same look as a real one and no meaning at all."""
        rows = [
            (_series(5500), AGS, "Classic Room"),
            (_series(90, currency="USD"), STERLING, "Classic Room"),
        ]
        cell = _cell(_grid(rows), STERLING, CLASSIC)
        assert cell.price == Decimal(90)
        assert cell.gap is None

    def test_the_other_column_shows_prices_and_no_gaps(self):
        """Other is where a name that states no tier lands. Two rooms are in it
        because neither could be placed, not because they are alike."""
        rows = [
            (_series(2000), AGS, "Machan"),
            (_series(9000), STERLING, "Treehouse"),
        ]
        grid = _grid(rows)
        assert [c.slug for c in grid.columns] == [OTHER]
        assert _cell(grid, STERLING, OTHER).price == Decimal(9000)
        assert _cell(grid, STERLING, OTHER).gap is None

    def test_a_gap_across_the_tax_line_is_flagged(self):
        """Treebo publishes an all-in figure and no pre-tax one, so with the
        switch off its price arrives marked. Subtracting it from a genuine
        pre-tax rate spans the tax line, and the marker on the two prices does
        not survive into the difference unless it is put there."""
        rows = [
            (_series(5500, basis="excl"), AGS, "Classic Room"),
            (_series(6100, basis="incl"), STERLING, "Classic Room"),
        ]
        cell = _cell(_grid(rows), STERLING, CLASSIC)
        assert cell.gap == Decimal(600)
        assert cell.mixed_basis

    def test_two_prices_on_the_same_basis_are_not_flagged(self):
        rows = [
            (_series(5500, basis="excl"), AGS, "Classic Room"),
            (_series(6100, basis="excl"), STERLING, "Classic Room"),
        ]
        assert not _cell(_grid(rows), STERLING, CLASSIC).mixed_basis

    def test_two_prices_that_both_fell_back_are_not_flagged(self):
        """Neither site published the component asked for, so both numbers are
        qualified in the same direction and the difference between them is
        not. Flagging it would put a warning on the one case that is fine."""
        rows = [
            (_series(5500, basis="incl"), AGS, "Classic Room"),
            (_series(6100, basis="incl"), STERLING, "Classic Room"),
        ]
        assert not _cell(_grid(rows), STERLING, CLASSIC).mixed_basis


class TestWithNothingOfOursToCompareTo:
    def test_no_own_property_still_prints_every_rate(self):
        """A page that went blank because a checkbox is unticked would send
        somebody to debug a fetcher. The rates are collected and worth reading
        while the tick box gets found."""
        grid = _grid(baseline=None)
        assert grid.baseline is None
        assert len(grid.rivals) == 3
        assert all(c.gap is None for r in grid.rivals for c in r.cells)

    def test_our_property_priced_on_no_room_tonight_leaves_the_rivals_intact(self):
        """Our own feed did not run. Everybody else's did, and their prices do
        not stop being true because ours are missing."""
        rows = [r for r in ROWS if r[1] is not AGS]
        grid = _grid(rows)
        assert grid.baseline is None
        assert _cell(grid, STERLING, CLASSIC).price == Decimal(4800)
        assert _cell(grid, STERLING, CLASSIC).gap is None

    def test_a_row_with_nothing_compared_claims_nothing(self):
        """``compared`` is what the template gates the 'under you in N of M'
        line on. At zero the line must not print: 'under you in 0 of 0' reads
        as 'they never undercut us' and means 'we could not compare'."""
        row = _grid(baseline=None).rivals[0]
        assert row.compared == 0
        assert row.cheaper_in == 0 and row.dearer_in == 0
