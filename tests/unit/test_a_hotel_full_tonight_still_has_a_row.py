"""A hotel that is sold out does not vanish from the comparison screen.

THE INCIDENT THIS FILE IS ABOUT
===============================
On the morning of 5 Sep -- a Saturday -- the price matrix showed three of ten
hotels. All ten had been fetched successfully forty minutes earlier and every
run was recorded as a success, so once again nothing had failed, nothing was
unowned, and no filter was set.

Eight of the ten were simply full. The screen split those eight in two, along
a line that is about the booking engine rather than about the hotel:

* Sterling's engine lists its rooms and prints "sold out" under each, so three
  unavailable rows were filed and the property appeared correctly.
* Seven others -- Aiosell, the local portal -- omit the rooms entirely on a
  full night. ``sold_out_detected`` is true, the fetcher rolls forward and
  prices the next night (``_with_rollover``), and NOTHING is filed under the
  night that was asked for.

The grid is built from ``price_series``, so those seven had no row at all,
which is exactly what a hotel nobody checked looks like.

The row therefore comes from the check run, which recorded the thing that
definitely happened: a successful fetch, for these dates, that found the hotel
sold out. No price is invented to go with it. A sold-out night quotes no rate,
and a placeholder series would need a meal plan and a refundability the empty
response never carried -- an offer key that could never match the real one on
the day the rooms came back.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.dashboard.routes import _sold_out_rows

USER = SimpleNamespace(id=7)
NIGHT = (date(2026, 9, 5), date(2026, 9, 6))
CHECKED_AT = datetime(2026, 9, 5, 9, 35)

ANANTHYAM = SimpleNamespace(id=1, name="Ananthyam Resort")


def _row(night, price, room, exclusive=None, taxes=None):
    """One (PriceSeries, hotel_id, room_name) triple as the query returns it."""
    series = SimpleNamespace(
        offer_key=f"k{room}{night}",
        check_in=night,
        check_out=night.replace(day=night.day + 1),
        currency="INR",
        current_price=price,
        last_price_exclusive=exclusive,
        last_taxes_fees=taxes,
        last_price_inclusive=None,
        last_checked_at=CHECKED_AT,
    )
    return (series, ANANTHYAM.id, room)
STERLING = SimpleNamespace(id=10, name="Sterling")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """Answers each execute() with the next prepared row set, keeping the SQL."""

    def __init__(self, *row_sets):
        self.row_sets = list(row_sets)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.row_sets.pop(0) if self.row_sets else [])


class TestAFullHotelKeepsItsRow:
    @pytest.mark.asyncio
    async def test_a_hotel_checked_and_found_full_is_listed(self):
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert [r["hotel"].name for r in rows] == ["Ananthyam Resort"]
        assert rows[0]["sold_out"] is True
        assert rows[0]["checked_at"] == CHECKED_AT

    @pytest.mark.asyncio
    async def test_no_price_is_invented_for_it(self):
        """The row carries no cells and no cheapest, because there is no rate.

        This is the whole reason the row is built here rather than by filing a
        placeholder into price_series: a sold-out night has a fact worth
        showing and no number worth storing.
        """
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["cells"] == []
        assert rows[0]["cheapest"] is None

    @pytest.mark.asyncio
    async def test_a_hotel_that_already_has_rooms_is_not_listed_twice(self):
        """Sterling's engine labels its rooms, so the grid already has it.

        Its check run is a sold-out one too, and without the exclusion the
        property would appear once with three sold-out rooms and again with
        none.
        """
        session = _Session([(STERLING, CHECKED_AT)], [])
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, {STERLING.id})
        assert rows == []

    @pytest.mark.asyncio
    async def test_nothing_full_costs_one_query(self):
        """The onward lookup is not run when there is nobody to run it for."""
        session = _Session([])
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows == []
        assert len(session.statements) == 1


class TestWhereTheRolledPricesWent:
    @pytest.mark.asyncio
    async def test_the_row_points_at_the_night_the_prices_landed_on(self):
        session = _Session(
            [(ANANTHYAM, CHECKED_AT)],
            [_row(date(2026, 9, 6), Decimal("7500"), "Superior Double")],
        )
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["next_priced"] == (date(2026, 9, 6), date(2026, 9, 7))

    @pytest.mark.asyncio
    async def test_the_earliest_night_after_this_one_wins(self):
        """A roll goes forward one night, never several.

        Later nights in the table belong to some other target, and offering
        one of those as "where the prices went" would be a guess dressed as a
        link.
        """
        session = _Session(
            [(ANANTHYAM, CHECKED_AT)],
            [
                _row(date(2026, 9, 6), Decimal("7500"), "Superior Double"),
                _row(date(2026, 9, 12), Decimal("4000"), "Superior Double"),
            ],
        )
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["next_priced"] == (date(2026, 9, 6), date(2026, 9, 7))
        assert [c["for_night"] for c in rows[0]["cells"]] == [date(2026, 9, 6)]

    @pytest.mark.asyncio
    async def test_a_roll_that_produced_nothing_leaves_no_link(self):
        """The rolled fetch is a bonus and is allowed to fail.

        When it does, the hotel is still full and still worth a row -- it just
        has nowhere to send anybody.
        """
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["next_priced"] is None


class TestWhatTheLookupAsksFor:
    @pytest.mark.asyncio
    async def test_only_a_successful_run_may_produce_a_row(self):
        """A failed fetch is not evidence that a hotel is full.

        Reporting one as sold out would turn every outage into a market of
        hotels with no rooms, which is the most confident possible way to be
        wrong.
        """
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        await _sold_out_rows(session, USER, *NIGHT, 2, set())
        sql = str(session.statements[0])
        assert "check_runs.status" in sql
        assert "check_runs.sold_out" in sql

    @pytest.mark.asyncio
    async def test_the_occupancy_comes_from_the_target(self):
        """A check run does not record how many guests it asked about.

        The target does, and without that clause a 2-adult grid would grow
        rows out of a 4-adult target's runs.
        """
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        await _sold_out_rows(session, USER, *NIGHT, 2, set())
        sql = str(session.statements[0])
        assert "monitor_targets.adults" in sql

    @pytest.mark.asyncio
    async def test_it_is_scoped_to_the_account_and_to_active_hotels(self):
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        await _sold_out_rows(session, USER, *NIGHT, 2, set())
        sql = str(session.statements[0])
        assert "hotels.owner_user_id" in sql
        assert "hotels.is_active" in sql


class TestTheRolledNightCarriesItsPrice:
    """A hotel with no cells needs a number on its row, or the row says nothing.

    These properties priced NO room for the night on screen -- their engines
    list nothing at all when full -- so unlike a hotel that labels its rooms
    sold out, there is no last rate to show beneath them. Without the rolled
    night's figure the only line on that row is the word "sold out", and a
    comparison screen that cannot say what a competitor charges has stopped
    comparing.
    """

    @pytest.mark.asyncio
    async def test_one_card_per_room_the_same_as_any_other_full_hotel(self):
        session = _Session(
            [(ANANTHYAM, CHECKED_AT)],
            [
                _row(date(2026, 9, 6), Decimal("7500"), "Deluxe Double"),
                _row(date(2026, 9, 6), Decimal("9000"), "Superior Double"),
            ],
        )
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        cells = rows[0]["cells"]
        assert [c["room_name"] for c in cells] == ["Deluxe Double", "Superior Double"]
        assert [c["price"] for c in cells] == [Decimal("7500"), Decimal("9000")]

    @pytest.mark.asyncio
    async def test_every_borrowed_price_carries_the_night_it_belongs_to(self):
        """Without it the figure reads as tonight's, and it is not.

        This is the trap absolute dates in the offer key exist to close, and
        there is no point being rigorous in the database and loose on screen.
        """
        session = _Session(
            [(ANANTHYAM, CHECKED_AT)],
            [_row(date(2026, 9, 6), Decimal("7500"), "Deluxe Double")],
        )
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["cells"][0]["for_night"] == date(2026, 9, 6)

    @pytest.mark.asyncio
    async def test_the_room_is_still_sold_out_for_the_night_on_screen(self):
        """The price is another night's; the availability is THIS night's.

        Marked unavailable so it can never be totalled into a "cheapest" or
        read as bookable tonight, whatever it costs on the night it was seen.
        """
        session = _Session(
            [(ANANTHYAM, CHECKED_AT)],
            [_row(date(2026, 9, 6), Decimal("7500"), "Deluxe Double")],
        )
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["cells"][0]["is_available"] is False
        assert rows[0]["cheapest"] is None

    @pytest.mark.asyncio
    async def test_a_hotel_with_no_priced_night_anywhere_keeps_the_plain_box(self):
        session = _Session([(ANANTHYAM, CHECKED_AT)], [])
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set())
        assert rows[0]["cells"] == []
        assert rows[0]["next_priced"] is None

    @pytest.mark.asyncio
    async def test_the_figures_follow_the_tax_switch(self):
        """They sit on a row beside cells rendered on the chosen basis.

        Two prices side by side, one inclusive of tax and one not, is the
        mismatch the whole switch exists to remove.
        """
        session = _Session(
            [(ANANTHYAM, CHECKED_AT)],
            [_row(date(2026, 9, 6), Decimal("6000"), "Deluxe Double",
                  exclusive=Decimal("6000"), taxes=Decimal("300"))],
        )
        rows = await _sold_out_rows(session, USER, *NIGHT, 2, set(), True)
        assert rows[0]["cells"][0]["price"] == Decimal("6300")
