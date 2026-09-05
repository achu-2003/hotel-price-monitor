"""Sterling's tax comes from the hotel's table, not from a rate we picked.

WHY THIS EXISTS
===============
Hotelzify quotes tax-exclusive. Its availability response prints
``priceBeforeTax`` 669 times for this property and carries no tax amount
anywhere, so Sterling was the one hotel of ten that could not be shown with
tax included -- the display fell back to the pre-tax rate marked "excl. tax".

The amount was never missing, only somewhere else: ``payments/v1/tax/list``
returns the property's OWN banded table, and for Sterling it reads 0% under
1,000, 5% up to 7,500, and 18% above.

THE DISTINCTION THIS FILE DEFENDS
=================================
Reading that table is reporting. Applying a GST rate WE chose would be
inference, and the band edges are exactly where inference goes wrong: this
property taxes a 7,400 room at 5% and a 7,600 room at 18%, and no single
assumed rate is right for both.

That the table is real, rather than a shape invented here, is corroborated
independently: Booking.com reported 375 of tax on a 7,500 room and 1,620 on a
9,000 room at another Yelagiri property, which is precisely what these bands
produce.
"""
from __future__ import annotations

from decimal import Decimal

from app.adapters.hotelzify import _tax_schedule

#: The live response for Sterling, trimmed to the fields that are read.
STERLING = {
    "status": 200,
    "data": [
        {"isActive": 1, "level": "room", "taxType": "percentage",
         "tax": 0, "priceFrom": 0, "priceTo": 999.99},
        {"isActive": 1, "level": "room", "taxType": "percentage",
         "tax": 5, "priceFrom": 1000, "priceTo": 7500},
        {"isActive": 1, "level": "room", "taxType": "percentage",
         "tax": 18, "priceFrom": 7500.01, "priceTo": None},
    ],
}


def band(**over):
    row = {"isActive": 1, "level": "room", "taxType": "percentage",
           "tax": 5, "priceFrom": 0, "priceTo": None}
    row.update(over)
    return {"data": [row]}


class TestTheBandsAreApplied:
    def test_the_rate_that_applies_is_the_one_for_that_price(self):
        s = _tax_schedule(STERLING)
        assert s.on(Decimal("6120")) == Decimal("306.00")     # 5%
        assert s.on(Decimal("9000")) == Decimal("1620.00")    # 18%

    def test_the_boundary_is_where_a_single_assumed_rate_would_be_wrong(self):
        """7,500 and 7,500.01 are taxed differently, by the hotel's own table.

        This is the case that makes the endpoint worth a third request. Any
        one rate applied across the portfolio is wrong on one side of this
        line, and confidently so.
        """
        s = _tax_schedule(STERLING)
        assert s.on(Decimal("7500")) == Decimal("375.00")
        assert s.on(Decimal("7500.01")) == Decimal("1350.00")

    def test_a_band_charging_nothing_is_a_real_answer(self):
        """0% under 1,000 is the hotel saying "no tax", and it is recorded.

        Distinct from a price the table does not reach, below.
        """
        assert _tax_schedule(STERLING).on(Decimal("500")) == Decimal("0.00")

    def test_an_open_top_band_has_no_ceiling(self):
        s = _tax_schedule(STERLING)
        assert s.on(Decimal("21990")) == Decimal("3958.20")

    def test_a_flat_charge_is_not_multiplied_by_the_price(self):
        s = _tax_schedule(band(taxType="fixed", tax=200))
        assert s.on(Decimal("6120")) == Decimal("200")

    def test_two_active_bands_over_one_price_are_summed(self):
        """A property may file a service charge as a second row.

        First-match would quietly under-report the bill, and a total that is
        short looks exactly like a total that is right.
        """
        rows = {"data": [
            {"isActive": 1, "level": "room", "taxType": "percentage",
             "tax": 5, "priceFrom": 0, "priceTo": None},
            {"isActive": 1, "level": "room", "taxType": "fixed",
             "tax": 100, "priceFrom": 0, "priceTo": None},
        ]}
        assert _tax_schedule(rows).on(Decimal("1000")) == Decimal("150.00")


class TestWhatIsIgnored:
    def test_an_inactive_row_is_not_charged(self):
        assert _tax_schedule(band(isActive=0)) is None

    def test_a_tax_filed_at_another_level_is_not_a_room_charge(self):
        """Adding a booking-level fee to a nightly rate overstates every room."""
        assert _tax_schedule(band(level="booking")) is None


class TestWhenTheTableCannotBeTrusted:
    """Refused whole, never row by row."""

    def test_an_unreadable_row_discards_the_whole_schedule(self):
        """A skipped row produces a total that looks complete and is short.

        The honest fallback -- no tax, and the display says "excl. tax" -- is
        better than a confident number missing a component.
        """
        rows = {"data": [
            {"isActive": 1, "level": "room", "taxType": "percentage",
             "tax": 5, "priceFrom": 0, "priceTo": 7500},
            {"isActive": 1, "level": "room", "taxType": "surge-pricing",
             "tax": 9, "priceFrom": 7500.01, "priceTo": None},
        ]}
        assert _tax_schedule(rows) is None

    def test_a_row_with_no_rate_is_not_read_as_zero(self):
        assert _tax_schedule(band(tax=None)) is None

    def test_an_empty_table_is_no_schedule(self):
        assert _tax_schedule({"status": 200, "data": []}) is None

    def test_a_response_that_is_not_the_table_is_refused(self):
        assert _tax_schedule({"unexpected": True}) is None
        assert _tax_schedule(None) is None


class TestAPriceTheTableDoesNotReach:
    def test_it_is_none_rather_than_zero(self):
        """A gap means the hotel has not said, which is not "nothing".

        Stored as zero it would render as a total identical to the rate --
        indistinguishable from a genuinely all-inclusive quote, and wrong in a
        way nobody could see.
        """
        s = _tax_schedule(band(priceFrom=1000, priceTo=7500))
        assert s.on(Decimal("500")) is None
        assert s.on(Decimal("9000")) is None
