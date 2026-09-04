"""Filtering the comparison screen to one room category.

The matrix answers "what is every hotel asking tonight". The question behind
it is nearly always narrower -- what is a suite going for, who is cheapest on
entry-level -- and the answer used to be spread across nine columns of room
names that do not line up between properties ("Club Room" here, "Classic
Room" there, "Deluxe Room" at the third).

The chips at the top narrow the grid to one category. What this file pins is
everything about that filter that could go quietly wrong: the counts on the
chips, which figure ends up in the Cheapest column, and which of the two very
different empty pages gets shown.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.dashboard.routes import _matrix_groups
from app.services.room_category import CLASSIC, SUITE, VILLA_2BR

CUTOFF = datetime(2026, 9, 4, tzinfo=UTC)

STERLING = SimpleNamespace(id=1, name="Sterling", is_own_property=True)
MGM = SimpleNamespace(id=2, name="MGM Whispering Nest", is_own_property=False)


def _series(price):
    return SimpleNamespace(
        offer_key=f"k{price}",
        current_price=price,
        currency="INR",
        is_available=True,
        last_changed_at=None,
        last_checked_at=datetime(2026, 9, 4, 6, tzinfo=UTC),
    )


#: Two properties, five rooms, four categories between them.
ROWS = [
    (_series(4200), STERLING, "Classic Room"),
    (_series(6800), STERLING, "Mountain View Classic Room"),
    (_series(9500), STERLING, "Junior Suite"),
    (_series(5100), MGM, "Club Room"),
    (_series(21000), MGM, "Villa 3 Bed Room with Living Room"),
]


class TestUnfiltered:
    def test_every_room_is_shown(self):
        grouped, _ = _matrix_groups(ROWS, CUTOFF, None)
        assert sum(len(e["cells"]) for e in grouped.values()) == 5

    def test_the_cheapest_is_the_cheapest_room_on_the_property(self):
        grouped, _ = _matrix_groups(ROWS, CUTOFF, None)
        assert grouped[STERLING.id]["cheapest"] == 4200

    def test_each_cell_carries_the_category_it_was_sorted_into(self):
        """The grid has to be able to say why a room is where it is, or the
        filter is a black box that drops rooms for reasons nobody can see."""
        grouped, _ = _matrix_groups(ROWS, CUTOFF, None)
        cells = {c["room_name"]: c for c in grouped[STERLING.id]["cells"]}
        assert cells["Junior Suite"]["category"] == SUITE
        assert cells["Junior Suite"]["category_label"] == "Suite"


class TestFiltered:
    def test_only_the_chosen_categorys_rooms_survive(self):
        grouped, _ = _matrix_groups(ROWS, CUTOFF, CLASSIC)
        names = sorted(c["room_name"] for e in grouped.values() for c in e["cells"])
        assert names == ["Classic Room", "Club Room", "Mountain View Classic Room"]

    def test_a_hotel_with_nothing_in_the_category_drops_out(self):
        """Not "present and empty". An empty row says only that the filter is
        on, which the chips already say, and the comparison reads better as
        the list of properties that actually sell the category."""
        grouped, _ = _matrix_groups(ROWS, CUTOFF, SUITE)
        assert list(grouped) == [STERLING.id]

    def test_the_cheapest_is_the_cheapest_of_what_is_shown(self):
        """THE BUG THIS PREVENTS: filtering to suites while the Cheapest
        column keeps reporting the property's 4,200 classic room. The column
        would then be comparing a figure that is nowhere on the row, and
        "cheapest suite tonight" -- the reason for filtering at all -- would
        be silently wrong for every property whose floor is a cheaper room.
        """
        grouped, _ = _matrix_groups(ROWS, CUTOFF, SUITE)
        assert grouped[STERLING.id]["cheapest"] == 9500

    def test_a_sold_out_room_is_never_the_cheapest(self):
        rows = [(_series(1), STERLING, "Classic Room")]
        rows[0][0].is_available = False
        grouped, _ = _matrix_groups(rows, CUTOFF, CLASSIC)
        assert grouped[STERLING.id]["cheapest"] is None


class TestTheCounts:
    def test_they_count_every_room_priced_tonight(self):
        _, counts = _matrix_groups(ROWS, CUTOFF, None)
        assert counts == {CLASSIC: 3, SUITE: 1, "villa-3br": 1}

    def test_they_do_not_change_when_a_category_is_chosen(self):
        """A chip has to keep saying "Suite 1" while the page is filtered to
        classic rooms. Counting only the surviving rooms would empty every
        other chip the moment one was clicked, and the only way back to the
        full grid would be the browser's Back button."""
        _, filtered = _matrix_groups(ROWS, CUTOFF, CLASSIC)
        _, unfiltered = _matrix_groups(ROWS, CUTOFF, None)
        assert filtered == unfiltered

    def test_a_category_nobody_sells_is_absent_rather_than_zero(self):
        """The page draws a chip per counted category, so a zero here would
        be a control that does nothing, and a row of them would bury the ones
        that work."""
        _, counts = _matrix_groups(ROWS, CUTOFF, None)
        assert VILLA_2BR not in counts


class TestTheTwoEmptyPages:
    """"Nothing collected" and "nothing in this category" are different.

    Showing the first when the second is true sends someone off to debug a
    fetcher that is working perfectly.
    """

    def test_a_filter_that_matches_nothing_still_leaves_the_night_priced(self):
        grouped, counts = _matrix_groups(ROWS, CUTOFF, VILLA_2BR)
        assert grouped == {}
        assert counts, "the night has prices; they are just in other categories"

    def test_a_night_with_no_prices_counts_nothing(self):
        grouped, counts = _matrix_groups([], CUTOFF, None)
        assert (grouped, counts) == ({}, {})
