"""Tests for price identity.

These are the most important tests in the suite. If ``offer_key`` is unstable,
every stored price history silently breaks; if it is too loose, prices from
different booking conditions get compared and every alert becomes a lie.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.offer_key import KEY_VERSION, OfferIdentity, compute_offer_key

BASE = dict(
    hotel_id=1,
    source_id=2,
    room_type_id=3,
    check_in=date(2026, 8, 20),
    check_out=date(2026, 8, 21),
    adults=2,
    children=0,
    meal_plan="Room Only",
    refundable=True,
    currency="INR",
)


def test_key_is_deterministic():
    assert compute_offer_key(**BASE) == compute_offer_key(**BASE)


def test_key_is_stable_across_runs():
    """Pinned literal: a change here means every stored history just broke.

    If this test fails after an intentional change, bump KEY_VERSION and write
    a migration that recomputes stored keys, then update this literal.
    """
    assert compute_offer_key(**BASE) == (
        "72c19e46409a00b739c41fd174990298e6201c5a257c0165c4ea689ffdb86eb0"
    )


def test_key_length_is_sha256_hex():
    assert len(compute_offer_key(**BASE)) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("hotel_id", 99),
        ("source_id", 99),
        ("room_type_id", 99),
        ("check_in", date(2026, 8, 21)),
        ("check_out", date(2026, 8, 22)),
        ("adults", 3),
        ("children", 1),
        ("meal_plan", "Breakfast Included"),
        ("refundable", False),
        ("currency", "USD"),
    ],
)
def test_every_booking_condition_changes_the_key(field, value):
    """Each condition in the requirement must produce a distinct series.

    This is the test that enforces "do not compare a 2-guest rate against a
    3-guest rate" at the structural level.
    """
    assert compute_offer_key(**{**BASE, field: value}) != compute_offer_key(**BASE)


def test_meal_plan_normalisation_is_case_and_space_insensitive():
    a = compute_offer_key(**{**BASE, "meal_plan": "Room Only"})
    b = compute_offer_key(**{**BASE, "meal_plan": "  room   ONLY "})
    assert a == b


def test_currency_is_case_insensitive():
    a = compute_offer_key(**{**BASE, "currency": "INR"})
    b = compute_offer_key(**{**BASE, "currency": "inr"})
    assert a == b


def test_missing_and_empty_meal_plan_collapse_to_one_series():
    """Both mean "the site told us no plan", so they share a series.

    Documented and deliberate: an empty string is not a meaningful plan, and
    splitting the series on it would fragment the history for no benefit.
    """
    none_key = compute_offer_key(**{**BASE, "meal_plan": None})
    empty_key = compute_offer_key(**{**BASE, "meal_plan": ""})
    # Empty normalises to the null sentinel too - documented, deliberate.
    assert none_key == empty_key


def test_unknown_refundability_differs_from_non_refundable():
    """Tri-state matters: "unknown" must not silently mean "non-refundable"."""
    unknown = compute_offer_key(**{**BASE, "refundable": None})
    non_refundable = compute_offer_key(**{**BASE, "refundable": False})
    refundable = compute_offer_key(**{**BASE, "refundable": True})
    assert len({unknown, non_refundable, refundable}) == 3


def test_canonical_string_is_readable_for_debugging():
    identity = OfferIdentity(**BASE)
    canonical = identity.canonical_string()
    assert canonical.startswith(f"{KEY_VERSION}|")
    assert "2026-08-20" in canonical
    assert "room only" in canonical


def test_nights_derived_from_dates():
    assert OfferIdentity(**BASE).nights == 1
    long_stay = OfferIdentity(**{**BASE, "check_out": date(2026, 8, 25)})
    assert long_stay.nights == 5
