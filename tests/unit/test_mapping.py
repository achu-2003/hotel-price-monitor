"""Config-driven JSON mapping.

These tests are the reason ``adapter_config`` is safe to edit in production:
every mapping a hotel can be given is exercisable against a recorded payload
with no browser and no network.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.adapters.mapping import (
    MISSING,
    dig,
    offer_from_mapping,
    render_template,
)
from app.core.errors import SchemaDriftError

PAYLOAD = {
    "data": {
        "rooms": [
            {
                "name": "Deluxe Room",
                "rates": [{"total": 2500, "taxes": 450, "board": "Room Only"}],
                "available": True,
                "roomsLeft": 3,
                "refundable": True,
            },
            {
                "name": "Suite",
                "rates": [{"total": "₹8,900", "taxes": None, "board": "Breakfast"}],
                "available": "SOLD_OUT",
            },
        ]
    }
}

MAPPING = {
    "room_name": "name",
    "price_inclusive": "rates.0.total",
    "taxes_fees": "rates.0.taxes",
    "meal_plan": "rates.0.board",
    "available": "available",
    "rooms_left": "roomsLeft",
    "refundable": "refundable",
}


class TestDig:
    def test_follows_dotted_path(self):
        assert dig(PAYLOAD, "data.rooms.0.name") == "Deluxe Room"

    def test_empty_path_returns_payload(self):
        """A config can say "the room list IS the response body"."""
        assert dig([1, 2], "") == [1, 2]
        assert dig([1, 2], None) == [1, 2]

    def test_missing_path_raises_by_default(self):
        # Refusing beats returning None: a silently-missing price field would
        # be indistinguishable from a genuinely absent one.
        with pytest.raises(SchemaDriftError):
            dig(PAYLOAD, "data.nope.0")

    def test_missing_path_returns_default_when_given(self):
        assert dig(PAYLOAD, "data.nope", None) is None
        assert dig(PAYLOAD, "data.nope", "x") == "x"

    def test_index_out_of_range_uses_default(self):
        assert dig(PAYLOAD, "data.rooms.9.name", None) is None

    def test_non_integer_index_into_list_uses_default(self):
        assert dig(PAYLOAD, "data.rooms.name", None) is None

    def test_descending_into_a_scalar_uses_default(self):
        assert dig(PAYLOAD, "data.rooms.0.name.deeper", None) is None

    def test_sentinel_is_distinct_from_none(self):
        """``None`` is a legitimate default, so MISSING cannot be None."""
        assert MISSING is not None


class TestRenderTemplate:
    def test_substitutes_dates_as_iso(self):
        rendered = render_template(
            "https://x/?in={check_in}&out={check_out}&ad={adults}",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            adults=2,
        )
        assert rendered == "https://x/?in=2026-08-20&out=2026-08-21&ad=2"

    def test_leaves_unknown_placeholders_alone(self):
        # str.format would raise here. A stray brace in a real URL must not
        # take a hotel offline.
        assert render_template("https://x/{weird}", adults=2) == "https://x/{weird}"

    def test_ignores_braces_that_are_not_placeholders(self):
        assert render_template("https://x/a{b}c", adults=2) == "https://x/a{b}c"


class TestOfferFromMapping:
    def test_maps_a_complete_room(self):
        offer = offer_from_mapping(PAYLOAD["data"]["rooms"][0], MAPPING)
        assert offer.raw_room_name == "Deluxe Room"
        assert offer.price_inclusive == Decimal("2500")
        assert offer.taxes_fees == Decimal("450")
        assert offer.meal_plan == "Room Only"
        assert offer.is_available is True
        assert offer.rooms_left == 3
        assert offer.refundable is True

    def test_parses_a_display_string_price(self):
        """Plenty of endpoints return "₹8,900" rather than a number."""
        offer = offer_from_mapping(PAYLOAD["data"]["rooms"][1], MAPPING)
        assert offer.price_inclusive == Decimal("8900")

    def test_sold_out_marker_is_falsy(self):
        offer = offer_from_mapping(PAYLOAD["data"]["rooms"][1], MAPPING)
        assert offer.is_available is False

    def test_available_room_without_a_price_is_drift(self):
        # The critical refusal: a listed room whose price we cannot find is a
        # broken mapping, not a free room.
        with pytest.raises(SchemaDriftError):
            offer_from_mapping(
                {"name": "Deluxe", "rates": [{}], "available": True}, MAPPING
            )

    def test_sold_out_room_without_a_price_is_fine(self):
        offer = offer_from_mapping(
            {"name": "Deluxe", "rates": [{}], "available": False}, MAPPING
        )
        assert offer.is_available is False
        assert offer.price_inclusive is None

    def test_unnamed_room_is_drift(self):
        with pytest.raises(SchemaDriftError):
            offer_from_mapping({"name": "  ", "rates": [{"total": 2500}]}, MAPPING)

    def test_derives_inclusive_from_exclusive_plus_taxes(self):
        offer = offer_from_mapping(
            {"name": "Deluxe", "net": 2500, "tax": 450, "available": True},
            {
                "room_name": "name",
                "price_exclusive": "net",
                "taxes_fees": "tax",
                "available": "available",
            },
        )
        assert offer.price_inclusive == Decimal("2950")

    def test_absent_refundable_stays_none(self):
        """Tri-state: "unknown" must not collide with "non-refundable"."""
        offer = offer_from_mapping(
            {"name": "Deluxe", "rates": [{"total": 2500}], "available": True}, MAPPING
        )
        assert offer.refundable is None

    def test_defaults_to_available_when_no_marker_configured(self):
        offer = offer_from_mapping(
            {"name": "Deluxe", "rates": [{"total": 2500}]},
            {"room_name": "name", "price_inclusive": "rates.0.total"},
        )
        assert offer.is_available is True

    def test_implausible_price_is_rejected(self):
        # The classic failure this catches: picking up a review count or a
        # room number instead of the nightly rate.
        with pytest.raises(SchemaDriftError):
            offer_from_mapping(
                {"name": "Deluxe", "rates": [{"total": 12}], "available": True}, MAPPING
            )

    def test_currency_is_upper_cased_and_truncated(self):
        offer = offer_from_mapping(
            {"name": "Deluxe", "rates": [{"total": 2500}], "cur": "inr"},
            {"room_name": "name", "price_inclusive": "rates.0.total", "currency": "cur"},
        )
        assert offer.currency == "INR"
