"""A full room says what it was asking before it filled.

WHY
===
"Sold out" alone is an absence. "Sold out, and they were asking 6,426" is the
thing a competitor decision actually rests on -- it is how you learn that the
property next door filled at a number you are still under.

The value was already being kept. ``ingest`` leaves ``current_price`` and the
tax components standing on a sold-out check, precisely so the last known rate
is not lost for however long the room stays full; only ``is_available`` moves.
Nothing but the matrix cell was refusing to show it.

WHAT MUST NOT HAPPEN
====================
It must never read as a price somebody can book. Two rules hold that line, and
both are asserted below:

* the figure never replaces the words -- "sold out" stays, and the rate sits
  under it prefixed with "was"
* a sold-out room is never the CHEAPEST on the row, however low it went. The
  cheapest column answers "who can I book tonight, and for how much", and a
  room nobody can have is not an answer to it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.dashboard.routes import _matrix_groups

CUTOFF = datetime(2026, 9, 4, tzinfo=UTC)
HOTEL = SimpleNamespace(id=1, name="Sterling", is_own_property=False)


def _series(price, available=True, exclusive=None, taxes=None, inclusive=None):
    return SimpleNamespace(
        offer_key=f"k{price}{available}",
        current_price=Decimal(str(price)),
        last_price_exclusive=exclusive,
        last_taxes_fees=taxes,
        last_price_inclusive=inclusive,
        currency="INR",
        is_available=available,
        last_changed_at=None,
        last_checked_at=datetime(2026, 9, 5, 6, tzinfo=UTC),
    )


class TestTheLastRateSurvivesTheSellOut:
    def test_a_sold_out_cell_still_carries_its_price(self):
        rows = [(_series(6120, available=False), HOTEL, "Classic Room")]
        grouped, _ = _matrix_groups(rows, CUTOFF, None)
        cell = grouped[HOTEL.id]["cells"][0]
        assert cell["is_available"] is False
        assert cell["price"] == Decimal("6120")

    def test_the_tax_switch_applies_to_it_too(self):
        """A "was" that is quietly pre-tax beside neighbours that are not is
        the same misreading, and the room being full does not fix it."""
        rows = [(
            _series(6120, available=False, exclusive=Decimal("6120"), taxes=Decimal("306")),
            HOTEL, "Classic Room",
        )]
        grouped, _ = _matrix_groups(rows, CUTOFF, None, True)
        assert grouped[HOTEL.id]["cells"][0]["price"] == Decimal("6426")


class TestItIsNeverAPriceYouCanBook:
    def test_a_sold_out_room_is_not_the_cheapest_on_the_row(self):
        """However low it went.

        The cheapest column answers "who can I book tonight, and for how
        much". A room nobody can have is not an answer, and letting one win
        would put a figure at the head of the row that no guest could pay.
        """
        rows = [
            (_series(2000, available=False), HOTEL, "Classic Room"),
            (_series(9000, available=True), HOTEL, "Junior Suite"),
        ]
        grouped, _ = _matrix_groups(rows, CUTOFF, None)
        assert grouped[HOTEL.id]["cheapest"] == Decimal("9000")

    def test_a_row_with_nothing_bookable_has_no_cheapest(self):
        """Not the lowest sold-out rate -- no answer at all.

        This is the Sterling row on a full Saturday: three rooms, three last
        prices worth reading, and nothing for sale. The column shows a dash.
        """
        rows = [
            (_series(6120, available=False), HOTEL, "Classic Room"),
            (_series(6324, available=False), HOTEL, "Classic room with Balcony"),
        ]
        grouped, _ = _matrix_groups(rows, CUTOFF, None)
        entry = grouped[HOTEL.id]
        assert entry["cheapest"] is None
        assert [c["price"] for c in entry["cells"]] == [Decimal("6120"), Decimal("6324")]

    def test_a_room_that_never_had_a_price_shows_none(self):
        """Nothing to be historical about, so nothing is claimed."""
        row = _series(0, available=False)
        row.current_price = None
        grouped, _ = _matrix_groups([(row, HOTEL, "Classic Room")], CUTOFF, None)
        assert grouped[HOTEL.id]["cells"][0]["price"] is None
