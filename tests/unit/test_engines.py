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
TREEBO = ("https://www.treebo.com/hotels-in-yelagiri/"
          "itsy-hotels-kurinji-stay-inn-with-swimming-pool-athanavoor-3965/"
          "?bookingSource=GoogleCPC&checkin=2026-08-19&checkout=2026-08-20"
          "&roomconfig=2-0&roomtype=maple&utm_source=googlehotelads")


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


class TestTreebo:
    """A brand site whose prices are DOM-only, by permission rather than choice.

    Treebo publishes prices through /api/ endpoints that its own robots.txt
    disallows, so the profile reads the rendered page instead. The test that
    matters most here is the last one: a future edit that "upgrades" this
    profile to the JSON endpoints would be reading what the site asked us not
    to, and nothing else in the suite would notice.
    """

    def test_is_recognised_with_its_property_code(self):
        d = detect(TREEBO)
        assert d is not None
        assert d.profile.key == "treebo"
        assert d.profile.adapter_key == "playwright_direct_site"
        # The trailing number in the slug, not a query parameter.
        assert d.external_id == "3965"

    def test_the_stored_template_varies_by_date(self):
        d = detect(TREEBO)
        assert d.is_complete
        assert "checkin={check_in}" in d.url_template
        assert "checkout={check_out}" in d.url_template
        assert "2026-08-19" not in d.url_template

    def test_occupancy_is_not_pinned_to_two_adults(self):
        """`roomconfig=2-0` packs both numbers into one value.

        Left alone it would keep asking for two adults while the target said
        four -- every price correct, every price for the wrong occupancy.
        """
        d = detect(TREEBO)
        assert "roomconfig={adults}-{children}" in d.url_template

    def test_the_card_is_anchored_on_an_id_not_a_hashed_class(self):
        """Treebo's CSS classes are styled-components hashes and rotate on
        every deploy; its `t-` ids are semantic and do not."""
        config = detect(TREEBO).profile.adapter_config
        assert config["room_card"] == "#t-roomTypes"
        assert "sc-" not in str(config["selectors"])

    def test_it_does_not_read_the_robots_disallowed_api(self):
        config = detect(TREEBO).profile.adapter_config
        assert "json_url_contains" not in config, (
            "Treebo's robots.txt disallows /api/, where its price endpoints "
            "live. This profile must read the rendered page only."
        )


@pytest.mark.parametrize("url,expected", [
    (AIOSELL, "aiosell"),
    (LETSBOOK, "ezee-letsbook"),
    (TREEBO, "treebo"),
    ("https://commonservice.ipms247.com/YCSAPIServices/booking/x?checkin=2026-01-01",
     "ezee-letsbook"),
])
def test_detection_table(url, expected):
    assert detect(url).profile.key == expected


class TestCombinedOccupancyParameter:
    """`?roomconfig=2-0` -- two numbers in one parameter."""

    def test_it_is_templated(self):
        url, changed = parameterise_url("https://x.example/book?roomconfig=2-0")
        assert "roomconfig={adults}-{children}" in url
        assert changed["roomconfig"] == "2-0 -> {adults}-{children}"

    def test_a_value_that_is_not_two_numbers_is_left_alone(self):
        """Only the shape we have actually seen is rewritten."""
        url, changed = parameterise_url("https://x.example/book?roomconfig=deluxe")
        assert "roomconfig=deluxe" in url
        assert "roomconfig" not in changed

    def test_other_parameters_are_untouched(self):
        url, _ = parameterise_url(
            "https://x.example/book?roomconfig=2-0&utm_source=googlehotelads"
        )
        assert "utm_source=googlehotelads" in url


class TestDatesInThePath:
    """Not every site puts its dates in a query string.

    bookmystay.io writes /rooms/43046/2026-08-19/2026-08-20/2/0. Invisible to a
    query-parameter scan, that URL was stored with the dates baked in -- so the
    target would have re-checked one fixed night forever -- and, because
    is_complete asks whether a {check_in} placeholder came out of here, the
    source was also labelled as one that cannot price a specific night. It
    prices per night perfectly well.
    """

    URL = ("https://bookmystay.io/rooms/43046/2026-08-19/2026-08-20/2/0"
           "?currency=INR&language=en")

    def test_both_dates_become_placeholders(self):
        url, changed = parameterise_url(self.URL)
        assert "/{check_in}/{check_out}/" in url
        assert "2026-08-19" not in url and "2026-08-20" not in url
        assert changed

    def test_the_rest_of_the_path_is_left_alone(self):
        url, _ = parameterise_url(self.URL)
        assert "/rooms/43046/" in url
        assert url.endswith("/2/0?currency=INR&language=en")

    def test_a_single_date_is_taken_as_the_check_in(self):
        url, _ = parameterise_url("https://x.example/book/2026-08-19/room")
        assert "/book/{check_in}/room" in url

    def test_a_path_without_dates_is_untouched(self):
        url, changed = parameterise_url("https://x.example/rooms/43046/2/0")
        assert url == "https://x.example/rooms/43046/2/0"
        assert changed == {}

    def test_a_number_that_is_not_a_date_is_not_rewritten(self):
        """Only the ISO shape counts -- an id is not a night."""
        url, _ = parameterise_url("https://x.example/hotel/20260819/rooms")
        assert "20260819" in url
