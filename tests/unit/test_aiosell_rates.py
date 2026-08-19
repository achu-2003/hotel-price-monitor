"""How an Aiosell rate entry is read into the three price components.

The field names on this endpoint cannot be trusted, and the consequence is not
cosmetic: whichever number lands in ``price_exclusive`` is what the dashboard
shows under the default basis, so a misread here puts a price on the screen
that appears on no page the guest can reach.

The payload shapes below are taken from a real property capture.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.adapters.aiosell import AiosellAdapter
from app.adapters.base import FetchContext
from app.core.errors import SchemaDriftError
from app.services.dates import StayWindow


def _context() -> FetchContext:
    return FetchContext(
        hotel_source_id=1,
        hotel_name="Roys Kozee Kaves",
        url="https://be.aiosell.com/book/b3cee25963",
        external_id="b3cee25963",
        stay=StayWindow(date(2026, 8, 19), date(2026, 8, 20)),
        adults=2,
        children=0,
        currency="INR",
    )


def _payload(rate_entry: dict) -> dict:
    return {
        "family-room": {
            "displayName": "Family Room",
            "available": True,
            "count": 3,
            "rates": {"EP": rate_entry},
        }
    }


def _only_offer(rate_entry: dict):
    offers = AiosellAdapter()._to_offers(_payload(rate_entry), _context())
    assert len(offers) == 1
    return offers[0]


class TestTaxOnTopShape:
    """`total_rate_tax_inclusive == total_rate`, tax exactly 5% OF it.

    Verbatim from the captured property: 2502.50 / 2502.50 / 125.125. A tax of
    125.125 is 5.000% of 2502.50 and 4.762% of 2627.625 — arithmetic that only
    works if 2502.50 is the pre-tax rate. Reading the field name literally and
    subtracting gives 2377.375, which is nobody's price.
    """

    ENTRY = {
        "total_rate": 2502.5,
        "total_rate_tax_inclusive": 2502.5,
        "total_tax": 125.125,
        "original_rate": 3850,
    }

    def test_the_published_rate_is_the_pre_tax_rate(self):
        assert _only_offer(self.ENTRY).price_exclusive == Decimal("2502.5")

    def test_the_all_in_price_is_the_sum(self):
        assert _only_offer(self.ENTRY).price_inclusive == Decimal("2627.625")

    def test_it_never_invents_a_rate_below_the_published_one(self):
        offer = _only_offer(self.ENTRY)
        assert offer.price_exclusive >= Decimal("2502.5")
        assert offer.price_exclusive != Decimal("2377.375")

    def test_the_dashboard_shows_the_number_on_the_booking_page(self):
        assert _only_offer(self.ENTRY).price_on("exclusive") == Decimal("2502.5")


class TestGenuinelyInclusiveShape:
    """A property where the two fields really do differ.

    This is the shape the field names promise, and it must keep working: the
    inclusive figure is used as published rather than recomputed.
    """

    ENTRY = {
        "total_rate": 2000.0,
        "total_rate_tax_inclusive": 2100.0,
        "total_tax": 100.0,
    }

    def test_both_components_are_taken_as_published(self):
        offer = _only_offer(self.ENTRY)
        assert offer.price_exclusive == Decimal("2000.0")
        assert offer.price_inclusive == Decimal("2100.0")
        assert offer.taxes_fees == Decimal("100.0")


class TestPartialPayloads:
    def test_only_an_inclusive_figure_still_yields_an_offer(self):
        offer = _only_offer({"total_rate_tax_inclusive": 3150.0, "total_tax": 150.0})
        assert offer.price_inclusive == Decimal("3150.0")
        assert offer.price_exclusive == Decimal("3000.0")

    def test_a_rate_with_no_tax_line_is_used_for_both(self):
        """Nothing is invented when the tax is not published."""
        offer = _only_offer({"total_rate": 1800.0})
        assert offer.price_exclusive == Decimal("1800.0")
        assert offer.price_inclusive == Decimal("1800.0")
        assert offer.price_on("exclusive") == Decimal("1800.0")

    def test_per_night_fields_are_used_when_the_totals_are_absent(self):
        offer = _only_offer(
            {"prices": [{"sellRate": 2502.5, "sellRateTaxInclusive": 2502.5}],
             "total_tax": 125.125}
        )
        assert offer.price_exclusive == Decimal("2502.5")

    def test_a_rate_carrying_only_a_struck_through_price_is_drift(self):
        """A rack rate is not a sellable rate.

        ``original_rate`` is the struck-through number; taking it as the price
        would publish a rate nobody can book. With no usable rate on any room
        the adapter refuses the whole payload rather than storing that.
        """
        with pytest.raises(SchemaDriftError):
            AiosellAdapter()._to_offers(_payload({"original_rate": 3850}), _context())
