"""Which price component a scraped room card lands in.

A DOM card usually shows one number. Whether that number is filed as the
pre-tax rate or the all-in price decides what the dashboard prints, and — once
a tax line is also captured — whether the tax gets subtracted from a rate that
never included it.

The cards below are the shapes of real monitored properties. No browser is
involved: the adapter only asks an element for `query_selector` and
`inner_text`, so a dict-backed stub exercises the real code path.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.adapters.base import FetchContext
from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter
from app.services.dates import StayWindow

SELECTORS = {"room_name": ".name", "price": ".price"}


class _Node:
    """The methods the adapter actually calls on a DOM element."""

    def __init__(self, text: str, children: dict | None = None, struck: bool = False):
        self._text = text
        self._children = children or {}
        self._struck = struck

    def inner_text(self) -> str:
        return self._text

    def query_selector(self, selector: str):
        return self._children.get(selector)

    def query_selector_all(self, selector: str):
        """Money fields are read through every match, not just the first, so
        that a struck-through rack rate can be skipped. See
        ``_price_text_in``."""
        match = self._children.get(selector)
        return [match] if match is not None else []

    def evaluate(self, _js: str) -> bool:
        """Stands in for the is-this-struck-through probe."""
        return self._struck


def _card(full_text: str, **parts: str) -> _Node:
    return _Node(full_text, {sel: _Node(txt) for sel, txt in parts.items()})


def _context() -> FetchContext:
    return FetchContext(
        hotel_source_id=1,
        hotel_name="Hotel Kumararraja Palace",
        url="https://live.ipms247.com/booking/book-rooms-hotelkumararrajapa",
        external_id=None,
        stay=StayWindow(date(2026, 8, 19), date(2026, 8, 20)),
        adults=2,
        children=0,
        currency="INR",
    )


def _offer(card, selectors=None):
    return PlaywrightDirectSiteAdapter()._offer_from_card(
        card, selectors or SELECTORS, _context()
    )


class TestCardDeclaringItsRateIsPreTax:
    """Verbatim from a monitored property: "Room Rates Exclusive of Tax"."""

    TEXT = (
        "Standard Room non A/C Room Capacity 3 1 Room Rates Exclusive of Tax "
        "Rs 3,200.00 Price for 1 Night 2 Adults , 0 Child, 1 Room 9 Rooms Left"
    )

    def _built(self):
        card = _card(self.TEXT, **{".name": "Standard Room non A/C", ".price": "Rs 3,200.00"})
        return _offer(card)

    def test_the_rate_is_filed_as_the_pre_tax_rate(self):
        offer = self._built()
        assert offer.price_exclusive == Decimal("3200")
        assert offer.price_inclusive is None

    def test_the_displayed_price_is_unchanged_on_either_basis(self):
        """The point of the fallback: relabelling must not move the number."""
        offer = self._built()
        assert offer.price_on("exclusive") == Decimal("3200")
        assert offer.price_on("inclusive") == Decimal("3200")


class TestCardThatSaysNothing:
    """No declaration, so nothing changes: the number is stored as before."""

    def test_price_stays_in_the_all_in_slot(self):
        card = _card(
            "Deluxe Room ₹4,275 per night 2 Adults",
            **{".name": "Deluxe Room", ".price": "₹4,275"},
        )
        offer = _offer(card)
        assert offer.price_inclusive == Decimal("4275")
        assert offer.price_exclusive is None
        assert offer.price_on("exclusive") == Decimal("4275")


class TestCardWithItsOwnExclusiveSelector:
    """A configured selector is evidence; a phrase never overrides it."""

    def test_the_configured_fields_win(self):
        card = _card(
            "Suite Room Rates Exclusive of Tax Rs 5,600.00 plus Rs 280.00 tax",
            **{
                ".name": "Suite Room",
                ".price": "Rs 5,880.00",
                ".net": "Rs 5,600.00",
                ".tax": "Rs 280.00",
            },
        )
        offer = _offer(
            card,
            {**SELECTORS, "price_exclusive": ".net", "taxes_fees": ".tax"},
        )
        assert offer.price_exclusive == Decimal("5600")
        assert offer.price_inclusive == Decimal("5880")
        assert offer.taxes_fees == Decimal("280")


class TestSoldOutCard:
    def test_a_sold_out_card_carries_no_price_at_all(self):
        card = _card(
            "Standard Room Rates Exclusive of Tax Sold Out",
            **{".name": "Standard Room", ".price": ""},
        )
        offer = _offer(card)
        assert offer.is_available is False
        assert offer.price_inclusive is None
        assert offer.price_exclusive is None


class TestTreeboCard:
    """The shape Treebo renders, which quotes tax-INCLUSIVE unlike the others.

    Four of the five monitored hotels publish a pre-tax rate; Treebo publishes
    the all-in figure and says so on the card. The parser has to follow the
    page rather than a house assumption, or this one hotel shows a number that
    appears nowhere on it.
    """

    TEXT = (
        "Available Rooms for Your Stay chevron_left chevron_right 10 Photos "
        "chevron_right Deluxe Room (Maple) 150 sq.ft. Queen Bed Free Wifi Ac "
        "Room Complimentary Toiletries +4 more ₹1,657 Incl. tax for 1 night "
        "1 room selected"
    )

    def _built(self):
        card = _card(
            self.TEXT,
            **{".name": "Deluxe Room (Maple)", ".price": "₹1,657"},
        )
        return _offer(card)

    def test_the_quoted_price_is_the_all_in_price(self):
        offer = self._built()
        assert offer.price_inclusive == Decimal("1657")
        assert offer.price_exclusive is None

    def test_the_dashboard_shows_what_the_page_shows(self):
        assert self._built().price_on("exclusive") == Decimal("1657")

    def test_the_room_name_survives_its_bracket(self):
        assert self._built().raw_room_name == "Deluxe Room (Maple)"
