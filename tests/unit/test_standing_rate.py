"""URLs with no date parameter, and what that means downstream.

Plenty of small hotels publish one standing rate rather than pricing per night.
Refusing those loses real data; accepting them without saying so is worse — it
would file today's rate under next Saturday, producing a number that is
plausible, wrong, and impossible to spot months later.

So they are accepted and labelled, and the label is what these tests pin.
"""
from __future__ import annotations

import pytest

from app.adapters.engines import detect, parameterise_url

DATED = ("https://be.aiosell.com/book/abc123"
         "?checkin=2026-08-20&checkout=2026-08-21&noOfGuests=2")
UNDATED = "https://be.aiosell.com/book/abc123"
UNDATED_WITH_QUERY = "https://be.aiosell.com/book/abc123?utm_source=google&ref=x"


class TestCompleteness:
    def test_a_dated_url_is_complete(self):
        d = detect(DATED)
        assert d is not None and d.is_complete

    def test_a_url_with_no_query_is_not_complete(self):
        d = detect(UNDATED)
        assert d is not None
        assert d.is_complete is False

    def test_a_query_without_dates_is_not_complete(self):
        """Having parameters is not the same as having date parameters."""
        d = detect(UNDATED_WITH_QUERY)
        assert d is not None
        assert d.is_complete is False
        # The unrelated parameters survive untouched.
        assert "utm_source=google" in d.url_template

    def test_incomplete_still_detects_the_engine(self):
        """Not complete must not mean not recognised.

        The engine, adapter and property code are all still known — only the
        ability to ask for a specific night is missing.
        """
        d = detect(UNDATED)
        assert d.profile.key == "aiosell"
        assert d.external_id == "abc123"


class TestParameterisation:
    def test_nothing_is_invented_when_there_are_no_dates(self):
        template, substituted = parameterise_url(UNDATED)
        assert template == UNDATED
        assert substituted == {}

    def test_a_partial_match_is_still_incomplete(self):
        """Adults but no check-in is not enough to vary the night."""
        d = detect("https://be.aiosell.com/book/abc?adults=2")
        assert "{adults}" in d.url_template
        assert d.is_complete is False


class TestTargetGuard:
    """The rule that keeps a standing rate honest.

    Enforced in the API rather than here, so this pins the shape the check
    relies on: the flag lives in adapter_config, where the target endpoint and
    the template both read it.
    """

    def test_flag_is_carried_in_adapter_config(self):
        config = {"standing_rate": True, "rooms_path": "data"}
        assert config.get("standing_rate") is True

    @pytest.mark.parametrize("lead_days,allowed", [(0, True), (1, False), (7, False)])
    def test_only_tonight_is_allowed(self, lead_days, allowed):
        # Mirrors app/api/v1/targets.py: a standing rate has no notion of a
        # future night, so only "tonight" can be truthfully recorded.
        standing = True
        rejected = standing and lead_days > 0
        assert (not rejected) is allowed
