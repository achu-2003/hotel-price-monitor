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
    detect_currency,
    looks_sold_out,
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


# ── urgency counters ─────────────────────────────────────────────────
def test_rooms_left_is_extracted_when_phrased_as_urgency():
    assert parse_rooms_left("Only 2 rooms left!") == 2


def test_a_bare_number_is_not_treated_as_stock():
    """Avoids reading a floor number or a guest count as remaining rooms."""
    assert parse_rooms_left("3") is None


def test_implausible_stock_counts_are_dropped():
    assert parse_rooms_left("Only 500 rooms left") is None
