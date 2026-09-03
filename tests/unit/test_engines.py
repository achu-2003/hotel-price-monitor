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


class TestBothDatesInOneParameter:
    """`?c=010926|020926` -- a whole stay in one opaque parameter.

    _PARAM_ALIASES matches on the parameter NAME, and a parameter holding both
    dates is usually called something that says nothing, like "c". So the URL
    was stored with the night baked in and every check afterwards re-read that
    same night: A R Thanga Kottai reported 9,995 for a room selling at 7,264,
    and the number never moved because it was a real price for 1 September.
    """

    STORED = (
        "https://www.cleartrip.com/hotels/details/-4534091"
        "?c=010926%7C020926&city=null&r=2%2C0"
    )

    def test_both_dates_are_templated(self):
        url, changed = parameterise_url(self.STORED)
        assert "c={check_in:%d%m%y}|{check_out:%d%m%y}" in url
        assert changed["c"].endswith("{check_in:%d%m%y}|{check_out:%d%m%y}")

    def test_the_source_is_no_longer_a_standing_rate(self):
        """is_complete asks whether a check-in placeholder came out of here."""
        url, _ = parameterise_url(self.STORED)
        assert "{check_in" in url

    def test_the_pair_decides_the_dialect(self):
        """010926 is 1 Sep day-first and 9 Jan month-first.

        Only day-first puts the check-out a night after the check-in, so only
        day-first describes a stay. Nothing else in the URL says which.
        """
        url, _ = parameterise_url("https://x.example/book?c=010926|020926")
        assert "%d%m%y" in url

    def test_an_unreadable_pair_is_left_exactly_as_pasted(self):
        """Rewriting it would ask for a different wrong night, which is worse.

        02-03-2026 to 03-03-2026 is one night read day-first and twenty-eight
        read month-first. Both are stays, so neither reading can be preferred.
        """
        url, changed = parameterise_url("https://x.example/book?c=020326|030326")
        assert "c=020326%7C030326" in url   # left alone, re-encoded normally
        assert "c" not in changed

    def test_a_value_that_is_not_a_date_pair_is_left_alone(self):
        url, changed = parameterise_url("https://x.example/book?c=deluxe|twin")
        assert "c=deluxe%7Ctwin" in url
        assert "c" not in changed

    def test_a_named_date_parameter_still_wins(self):
        """A parameter this function can already read is not second-guessed."""
        url, _ = parameterise_url(
            "https://x.example/book?checkin=2026-09-01&checkout=2026-09-02"
        )
        assert "checkin={check_in}" in url
        assert "checkout={check_out}" in url


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


class TestAnEngineThatDoesNotSpeakISO:
    """A URL whose dates are written 03-09-2026, not 2026-09-03.

    bookingsmaker asks for ``gindate=03-09-2026``. Two ways to get this wrong,
    and the system managed both in turn:

    1. Not parameterising it at all, because "gindate" was in none of the alias
       lists. The URL was stored verbatim, ``is_complete`` said the source
       could not price a specific night, and the monitor re-checked whichever
       night the operator happened to paste -- forever, and looking healthy.

    2. Parameterising it as ISO. The placeholder renders 2026-09-03, the site
       does not understand it, and the answer is a page for some other night
       or no page at all.

    So the placeholder carries the engine's own spelling: ``{check_in:%d-%m-%Y}``.
    """

    URL = ("https://www.bookingsmaker.com/ibe/rooms.php?ghotelid=4594"
           "&gindate=03-09-2026&goutdate=04-09-2026&adults=2")

    def test_the_dates_become_placeholders_at_all(self):
        template, substituted = parameterise_url(self.URL)
        assert "03-09-2026" not in template
        assert "04-09-2026" not in template
        assert "gindate" in substituted

    def test_the_placeholder_carries_the_engines_own_date_format(self):
        template, _ = parameterise_url(self.URL)
        assert "gindate={check_in:%d-%m-%Y}" in template
        assert "goutdate={check_out:%d-%m-%Y}" in template

    def test_the_format_survives_url_encoding(self):
        """%25d would render a literal percent, not a day of the month."""
        template, _ = parameterise_url(self.URL)
        assert "%25" not in template
        assert "%3A" not in template

    def test_a_literal_percent_elsewhere_is_still_encoded(self):
        """Sparing "%" for the placeholder must not spare it for real data."""
        from urllib.parse import parse_qsl, urlparse

        template, _ = parameterise_url(self.URL + "&promo=SAVE%2550")
        assert dict(parse_qsl(urlparse(template).query))["promo"] == "SAVE%50"

    def test_the_source_can_price_a_specific_night(self):
        """is_complete reads the placeholder, and nearly missed the formatted one."""
        from app.adapters.engines import Detection, EngineProfile

        template, substituted = parameterise_url(self.URL)
        detection = Detection(
            profile=EngineProfile(
                key="x", display_name="x", adapter_key="playwright_direct_site",
                domains=("www.bookingsmaker.com",), adapter_config={},
            ),
            url_template=template, external_id=None, substituted=substituted,
        )
        assert detection.is_complete is True

    def test_an_iso_url_is_left_in_iso(self):
        """The format is only added where it is needed."""
        template, _ = parameterise_url(AIOSELL)
        assert "{check_in}" in template
        assert "{check_in:" not in template


class TestWhichWayRoundADateIsRead:
    """03-09-2026 is 3 September here and 9 March in the United States.

    The pair decides it: a check-out is a night or two after its check-in, so
    normally only one reading describes a stay at all. Where both do, neither
    is chosen -- a source that cannot be re-dated is honest, one re-dated into
    the wrong month is not.
    """

    def test_day_first_is_read_when_only_day_first_is_a_stay(self):
        from app.adapters.engines import _date_format_of

        # Month-first would be 9 Mar -> 9 Apr, which is not a hotel booking.
        assert _date_format_of("03-09-2026", "04-09-2026") == "%d-%m-%Y"

    def test_month_first_is_read_when_only_month_first_is_a_stay(self):
        from app.adapters.engines import _date_format_of

        # Day-first would be 9 Mar -> 9 Apr.
        assert _date_format_of("09/03/2026", "09/04/2026") == "%m/%d/%Y"

    def test_a_pair_that_reads_as_a_stay_both_ways_is_refused(self):
        """2 Mar -> 3 Mar, or 3 Feb -> 3 Mar. Both are stays; neither wins."""
        from app.adapters.engines import _date_format_of

        assert _date_format_of("02-03-2026", "03-03-2026") is None

    def test_a_pair_that_reads_as_a_stay_neither_way_is_refused(self):
        from app.adapters.engines import _date_format_of

        assert _date_format_of("03-09-2026", "03-09-2027") is None
        assert _date_format_of("not-a-date", "also-not") is None

    def test_an_ambiguous_pair_leaves_the_url_alone_rather_than_guessing(self):
        url = ("https://x.example/book?checkin=02-03-2026"
               "&checkout=03-03-2026&adults=2")
        template, _ = parameterise_url(url)
        assert "02-03-2026" in template
        assert "{check_in" not in template

    def test_a_lone_date_is_read_when_it_can_only_mean_one_thing(self):
        """No check-out to compare against, but there is no month 25."""
        url, _ = parameterise_url("https://x.example/book?checkin=25-12-2026&adults=2")
        assert "checkin={check_in:%d-%m-%Y}" in url

    def test_a_lone_date_that_could_mean_two_things_is_left_alone(self):
        url, _ = parameterise_url("https://x.example/book?checkin=03-09-2026&adults=2")
        assert "checkin=03-09-2026" in url
        assert "{adults}" in url  # the rest of the URL is still parameterised

    def test_an_iso_date_written_with_slashes_keeps_its_slashes(self):
        """{check_in} alone would render 2026-09-03 to a site that wrote 2026/09/03."""
        url, _ = parameterise_url(
            "https://x.example/book?checkin=2026/09/03&checkout=2026/09/04"
        )
        assert "checkin={check_in:%Y/%m/%d}" in url
        assert "checkout={check_out:%Y/%m/%d}" in url


class TestAUrlThatKeepsEverythingAfterTheHash:
    """A single-page booking engine routes on the fragment.

    swiftbook hands out ``/inst/#home?propertyId=...&checkIn=2026-08-25``.
    urlparse puts none of that in `query`, so nothing was parameterised: the
    URL was stored with 25 August baked in, and is_complete reported a source
    that cannot price a specific night -- of a site that prices per night
    perfectly well. Every check would have re-read one night forever while
    reporting success, which is the failure this whole function exists to stop.
    """

    URL = ("https://www.swiftbook.io/inst/#home"
           "?propertyId=362NTRtI6w1gSaEHad4Im74Q1Njg=&JDRN=Y"
           "&checkIn=2026-08-25&checkOut=2026-08-26&currency=INR"
           "&adult=2&child=0&source=mapresults")

    def test_the_dates_become_placeholders(self):
        template, substituted = parameterise_url(self.URL)
        assert "checkIn={check_in}" in template
        assert "checkOut={check_out}" in template
        assert "2026-08-25" not in template
        assert substituted["checkIn"] == "2026-08-25 -> {check_in}"

    def test_occupancy_in_the_fragment_is_parameterised_too(self):
        template, _ = parameterise_url(self.URL)
        assert "adult={adults}" in template
        assert "child={children}" in template

    def test_the_source_can_price_a_specific_night(self):
        from app.adapters.engines import Detection, EngineProfile

        template, substituted = parameterise_url(self.URL)
        detection = Detection(
            profile=EngineProfile(
                key="x", display_name="x", adapter_key="playwright_direct_site",
                domains=("www.swiftbook.io",), adapter_config={},
            ),
            url_template=template, external_id=None, substituted=substituted,
        )
        assert detection.is_complete is True

    def test_a_base64_property_id_survives_byte_for_byte(self):
        """The fragment is read by the page's own JavaScript, not by a server.

        Percent-encoding the "=" that ends a base64 id produces a URL the page
        cannot open, so the fragment is rewritten as text and only the values
        being replaced are touched.
        """
        template, _ = parameterise_url(self.URL)
        assert "propertyId=362NTRtI6w1gSaEHad4Im74Q1Njg=" in template
        assert "%3D" not in template

    def test_everything_else_in_the_fragment_is_left_alone(self):
        template, _ = parameterise_url(self.URL)
        assert "#home?" in template
        assert "JDRN=Y" in template
        assert "currency=INR" in template
        assert "source=mapresults" in template

    def test_it_renders_back_to_a_url_the_site_can_open(self):
        from datetime import date

        from app.adapters.mapping import render_template

        template, _ = parameterise_url(self.URL)
        rendered = render_template(
            template, check_in=date(2026, 12, 24), check_out=date(2026, 12, 25),
            adults=2, children=0, rooms=1, nights=1,
        )
        assert rendered == (
            "https://www.swiftbook.io/inst/#home"
            "?propertyId=362NTRtI6w1gSaEHad4Im74Q1Njg=&JDRN=Y"
            "&checkIn=2026-12-24&checkOut=2026-12-25&currency=INR"
            "&adult=2&child=0&source=mapresults"
        )

    def test_a_plain_fragment_is_not_disturbed(self):
        """#section is an anchor, not a parameter list."""
        url = "https://x.example/book?checkin=2026-08-18#rooms"
        template, _ = parameterise_url(url)
        assert template.endswith("#rooms")
        assert "checkin={check_in}" in template
