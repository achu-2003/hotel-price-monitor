"""Tests for price parsing.

The bounds checks matter more than the happy path. The classic scraper failure
is picking up the wrong element — a review count, a room number, a discount
percentage — and storing it as a price. These tests pin down that we refuse
instead.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.parsing import (
    declared_tax_basis,
    detect_currency,
    looks_sold_out,
    parse_added_taxes,
    parse_price,
    parse_price_or_none,
    parse_rooms_left,
)
from app.core.errors import SchemaDriftError

D = Decimal


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2500", D("2500")),
        ("2,500", D("2500")),
        ("₹2,500", D("2500")),
        ("Rs. 2,500", D("2500")),
        ("INR 2500", D("2500")),
        ("₹ 2,500.00", D("2500.00")),
        ("2,500.50", D("2500.50")),
        ("  ₹2,500 per night  ", D("2500")),
        ("₹1,23,456", D("123456")),          # Indian grouping
        ("2.500", D("2500")),                 # European grouping
        ("From ₹3,299", D("3299")),
    ],
)
def test_prices_parse_from_real_world_formats(raw, expected):
    assert parse_price(raw) == expected


def test_indian_lakh_grouping():
    assert parse_price("₹2,50,000") == D("250000")


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_input_raises_rather_than_returning_zero(raw):
    with pytest.raises(SchemaDriftError):
        parse_price(raw)


def test_text_without_a_number_raises():
    with pytest.raises(SchemaDriftError, match="No number found"):
        parse_price("Sold Out")


# ── the bounds checks that catch wrong-element bugs ───────────────────
def test_a_review_count_is_rejected_as_a_price():
    """"4.5" from a rating widget must never become a room rate."""
    with pytest.raises(SchemaDriftError, match="outside the plausible range"):
        parse_price("4.5")


def test_a_discount_percentage_is_rejected():
    with pytest.raises(SchemaDriftError, match="outside the plausible range"):
        parse_price("20% off")


def test_an_absurdly_large_number_is_rejected():
    with pytest.raises(SchemaDriftError, match="outside the plausible range"):
        parse_price("999999999")


def test_the_error_explains_the_likely_cause():
    """The message is what an operator reads at 7am. It should point at the fix."""
    with pytest.raises(SchemaDriftError) as excinfo:
        parse_price("12")
    assert "selector" in str(excinfo.value)


def test_bounds_are_overridable_for_multi_night_totals():
    assert parse_price("₹95", min_value=D("50")) == D("95")


# ── optional fields ──────────────────────────────────────────────────
def test_optional_parse_returns_none_instead_of_raising():
    assert parse_price_or_none("not a price") is None


def test_optional_parse_allows_zero_taxes():
    assert parse_price_or_none("₹0") == D("0")


# ── currency ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [("₹2500", "INR"), ("Rs. 2500", "INR"), ("$120", "USD"), ("€99", "EUR"), ("2500", "INR")],
)
def test_currency_detection(raw, expected):
    assert detect_currency(raw) == expected


# ── availability ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    ["Sold Out", "SOLD OUT", "No rooms available", "Fully booked",
     "Not Available for these dates", "No availability"],
)
def test_sold_out_markers_are_recognised(text):
    assert looks_sold_out(text)


def test_a_normal_price_line_is_not_sold_out():
    assert not looks_sold_out("Deluxe Room ₹2,500 per night")


@pytest.mark.parametrize(
    "text",
    [
        # The one that actually happened: a booking page prompting the visitor
        # to pick a room was read as the hotel declaring itself full, and a
        # room on sale at 2,017 was recorded as sold out.
        "No rooms selected yet. Choose a room to continue your booking.",
        "0 rooms selected",
        "No rooms selected",
        # Amenity and policy copy sitting in the same card as the price.
        "Airport shuttle not available at this property",
        "Breakfast unavailable on Sundays",
        "Free cancellation unavailable for this rate",
    ],
)
def test_interface_copy_is_not_mistaken_for_sold_out(text):
    """Inventing a sold-out is far worse than missing one.

    A missed sold-out means no price is found, which raises schema drift and
    puts a visible error in front of a person. An invented one writes a
    confident business fact, notifies whoever watches that hotel, and is
    indistinguishable from a correct answer.
    """
    assert not looks_sold_out(text)


def test_a_real_sold_out_still_reads_as_sold_out_beside_other_copy():
    """Narrowing the markers must not go so far that a genuine one is missed."""
    assert looks_sold_out(
        "Deluxe Room · Free wifi · No rooms available for these dates"
    )


# ── urgency counters ─────────────────────────────────────────────────
def test_rooms_left_is_extracted_when_phrased_as_urgency():
    assert parse_rooms_left("Only 2 rooms left!") == 2


def test_a_bare_number_is_not_treated_as_stock():
    """Avoids reading a floor number or a guest count as remaining rooms."""
    assert parse_rooms_left("3") is None


def test_implausible_stock_counts_are_dropped():
    assert parse_rooms_left("Only 500 rooms left") is None


class TestDeclaredTaxBasis:
    """Reading a page's own statement about which side of the tax it quotes.

    The consequence of getting this wrong is a rate with the tax deducted from
    it twice, so nothing here guesses: only an explicit phrase counts.
    """

    # Verbatim from a monitored property.
    CARD = (
        "Standard Room non A/C Room Capacity 3 1 Room Rates Exclusive of Tax "
        "Rs 3,200.00 Price for 1 Night 2 Adults , 0 Child, 1 Room add to "
        "compare Add To Compare Room Info Enquire 9 Rooms Left Add Room"
    )

    def test_reads_a_real_cards_declaration(self):
        assert declared_tax_basis(self.CARD) == "exclusive"

    @pytest.mark.parametrize(
        "text",
        ["Rs 3,200 + taxes", "Rs 3,200 plus taxes", "₹3,200 excl. tax",
         "Tariff before tax", "Rate 3200, taxes extra"],
    )
    def test_recognises_the_common_phrasings(self, text):
        assert declared_tax_basis(text) == "exclusive"

    @pytest.mark.parametrize(
        "text",
        ["₹3,360 inclusive of tax", "Rs 3,360 including taxes",
         "Total 3360 (tax included)", "₹3,360 inclusive of all taxes"],
    )
    def test_recognises_the_inclusive_phrasings(self, text):
        assert declared_tax_basis(text) == "inclusive"

    def test_silence_is_not_a_declaration(self):
        assert declared_tax_basis("Deluxe Room ₹3,200 per night") is None
        assert declared_tax_basis("") is None
        assert declared_tax_basis(None) is None

    def test_a_card_stating_both_declares_nothing(self):
        """Two figures described, one captured — which one is unknowable."""
        both = "Rs 3,200 exclusive of tax. Rs 3,360 inclusive of tax."
        assert declared_tax_basis(both) is None

    def test_line_breaks_and_spacing_do_not_hide_the_phrase(self):
        card = "Room Rates\n  Exclusive   of Tax\nRs 3,200.00"
        assert declared_tax_basis(card) == "exclusive"


class TestATaxStatedWithItsAmount:
    """"₹9,995 + ₹2,019 taxes & fees" — the phrasing every OTA card uses.

    The literal markers look for "+ taxes", and a card that prints the figure
    it is adding puts the number in between, so the most explicit statement a
    page can make was the one that read as silence. The headline then went in
    as the all-in price with the stated tax discarded, and a dashboard showed
    a pre-tax rate as if it were the bill.
    """

    # Verbatim from a Cleartrip room card.
    CARD = (
        "1/4 Nandhavanam Room Double Bed · 500 sq.ft · 2 Adults Blanket Water "
        "bottle Attached washroom Room with Breakfast & Dinner Cancellation "
        "charges apply ₹10,995 + ₹2,019 taxes & fees night ₹11,219 2% off Book"
    )

    def test_the_card_is_read_as_a_pre_tax_rate(self):
        assert declared_tax_basis(self.CARD) == "exclusive"

    def test_the_stated_tax_is_kept(self):
        assert parse_added_taxes(self.CARD) == D("2019")

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("/ night + ₹1,836 taxes & fees", D("1836")),
            ("+ Rs 450 tax", D("450")),
            ("plus ₹1,08,363 taxes", D("108363")),
            ("₹5,000 + 900 GST", D("900")),
            ("+ ₹250 fees", D("250")),
        ],
    )
    def test_it_reads_the_amount_whatever_the_wording(self, text, expected):
        assert parse_added_taxes(text) == expected
        assert declared_tax_basis(text) == "exclusive"

    @pytest.mark.parametrize(
        "text",
        [
            # The trailing word is what makes it a tax line. Without it this
            # is an amenity count, and reading 13 as a tax would put a
            # nonsense figure on the row.
            "Blackout curtains Flat-screen TV Feather pillow TV + 13 more",
            "Deluxe Room ₹3,200 per night",
            "",
        ],
    )
    def test_it_refuses_anything_that_is_not_a_stated_tax(self, text):
        assert parse_added_taxes(text) is None

    def test_none_is_not_a_string(self):
        assert parse_added_taxes(None) is None

    def test_a_card_stating_both_still_declares_nothing(self):
        # The existing rule survives the new phrasing: a page describing two
        # different figures has not told us which one we scraped.
        both = "₹3,200 + ₹576 taxes; ₹3,776 inclusive of tax"
        assert declared_tax_basis(both) is None
