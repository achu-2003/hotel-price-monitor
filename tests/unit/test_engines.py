"""Recognising a booking engine from a pasted URL.

The value of auto-detection is that adding a hotel becomes one paste. The risk
is that it quietly gets something wrong — a mis-detected engine or, worse, a
URL stored with its dates baked in, which keeps fetching the same night while
looking perfectly healthy. These pin both.
"""
from __future__ import annotations

import pytest

from app.adapters.engines import ENGINES, detect, known_engines, parameterise_url

AIOSELL = ("https://be.aiosell.com/book/b3cee25963"
           "?checkin=2026-08-18&checkout=2026-08-19&noOfGuests=2&noOfRooms=1")
LETSBOOK = ("https://letsbook.me/booking/hotelgoldennest"
            "?checkin=2026-08-18&checkout=2026-08-19&adults=2&children=0")


class TestDetection:
    def test_aiosell_is_recognised_with_its_property_code(self):
        d = detect(AIOSELL)
        assert d is not None
        assert d.profile.key == "aiosell"
        assert d.profile.adapter_key == "aiosell"
        # Lifted from /book/<code>, so the operator never types it.
        assert d.external_id == "b3cee25963"

    def test_letsbook_is_recognised_and_uses_the_browser_adapter(self):
        d = detect(LETSBOOK)
        assert d is not None
        assert d.profile.key == "ezee-letsbook"
        # Its API needs a token the page mints, so the page is driven and the
        # JSON it requests is read — no token is extracted or replayed.
        assert d.profile.adapter_key == "playwright_direct_site"
        assert d.profile.adapter_config["rooms_path"] == "data"

    def test_an_unknown_engine_returns_none_rather_than_guessing(self):
        """The whole point: no configuration is invented for an unseen site."""
        assert detect("https://some-unknown-engine.example/book?d=1") is None

    def test_garbage_input_does_not_raise(self):
        assert detect("not a url") is None
        assert detect("") is None

    def test_detection_is_by_host_not_by_the_whole_url(self):
        # A path that merely mentions another engine must not fool it.
        assert detect("https://be.aiosell.com/book/x/letsbook.me?checkin=2026-01-01") \
            .profile.key == "aiosell"


class TestParameterisation:
    def test_dates_become_placeholders(self):
        """Stored verbatim, a pasted URL pins one night forever.

        The checks would keep succeeding and the prices would silently stop
        moving — the exact failure the Health tab's "gone quiet" alarm exists
        to catch, arriving here instead as bad data.
        """
        template, substituted = parameterise_url(AIOSELL)
        assert "{check_in}" in template
        assert "{check_out}" in template
        assert "2026-08-18" not in template
        assert "checkin" in substituted

    def test_occupancy_is_parameterised_across_naming_styles(self):
        # noOfGuests, adults, noOfAdults all mean the same thing.
        assert "{adults}" in parameterise_url(AIOSELL)[0]
        assert "{adults}" in parameterise_url(LETSBOOK)[0]

    def test_placeholders_are_not_percent_encoded(self):
        """%7Bcheck_in%7D would never be substituted by the adapter."""
        template, _ = parameterise_url(AIOSELL)
        assert "%7B" not in template

    def test_unknown_parameters_are_left_alone(self):
        template, _ = parameterise_url(
            "https://be.aiosell.com/book/x?checkin=2026-08-18&utm_source=google"
        )
        assert "utm_source=google" in template

    def test_a_url_with_no_query_is_returned_unchanged(self):
        url = "https://be.aiosell.com/book/abc"
        assert parameterise_url(url) == (url, {})

    def test_a_url_without_dates_is_flagged_incomplete(self):
        """Refused at the API, rather than silently monitoring one fixed night."""
        d = detect("https://be.aiosell.com/book/abc?utm=1")
        assert d is not None
        assert d.is_complete is False


class TestProfiles:
    def test_every_profile_is_internally_consistent(self):
        for profile in ENGINES:
            assert profile.key and profile.display_name
            assert profile.domains, f"{profile.key} matches nothing"
            assert profile.rate_limit_per_min >= 1

    def test_every_adapter_key_actually_exists(self):
        """A profile naming an adapter that is not registered would fail at
        fetch time, long after the hotel looked successfully configured."""
        from app.adapters import registry

        for profile in ENGINES:
            assert profile.adapter_key in registry.available_keys(), profile.key

    def test_engine_keys_are_unique(self):
        keys = [e.key for e in ENGINES]
        assert len(keys) == len(set(keys))

    def test_known_engines_is_serialisable_for_the_dashboard(self):
        listed = known_engines()
        assert listed and all("domains" in e for e in listed)


@pytest.mark.parametrize("url,expected", [
    (AIOSELL, "aiosell"),
    (LETSBOOK, "ezee-letsbook"),
    ("https://commonservice.ipms247.com/YCSAPIServices/booking/x?checkin=2026-01-01",
     "ezee-letsbook"),
])
def test_detection_table(url, expected):
    assert detect(url).profile.key == expected
