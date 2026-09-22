"""The link a hotel was found through can decide what it is charged.

Booking.com reads the affiliate parameters on an incoming URL and serves that
channel's deal. A link captured from Google Hotel Ads carries the metasearch
campaign, and on 22 Sep 2026 that was the difference between:

    as captured   Standard Double  6,358 + 361   "25% off"
    cleaned       Standard Double  7,225 + 361   "15% off"

Our own hotel was saved with such a link and the competitor was not, so the
comparison ran between two different rate channels. The repricing rule then
inherited it: it divides the target by our own price to reach an RMS rate, so
a price read twelve percent low writes a rate twelve percent high, and a rule
set to sit a hundred rupees UNDER the competitor put us five hundred OVER on
the live site.

Pure: a string in, a string out. No page, no network.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.adapters.playwright_direct_site import _public_rate_url

METASEARCH = (
    "https://www.booking.com/hotel/in/ags-holiday-resorts-yelagiri1.en-gb.html"
    "?aid=1288252&label=metagha-link-MRIN-hotel-6805954_dev-desktop_los-1"
    "&checkin=2026-09-22&checkout=2026-09-23&group_adults=2&no_rooms=1"
    "&sb_price_type=total&type=total&ucfs=1"
)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query, keep_blank_values=True)


class TestTheCampaignIsDropped:
    def test_the_partner_id_goes(self):
        assert "aid" not in _query(_public_rate_url(METASEARCH))

    def test_the_campaign_label_goes(self):
        assert "label" not in _query(_public_rate_url(METASEARCH))

    def test_it_is_still_the_same_page(self):
        cleaned = urlparse(_public_rate_url(METASEARCH))
        assert cleaned.netloc == "www.booking.com"
        assert cleaned.path.endswith("ags-holiday-resorts-yelagiri1.en-gb.html")


class TestNothingElseIsTouched:
    """The stay, the occupancy and the price basis live in the same query
    string. A general "tidy the URL" would take the page somewhere else."""

    def test_the_night_survives(self):
        q = _query(_public_rate_url(METASEARCH))
        assert q["checkin"] == ["2026-09-22"]
        assert q["checkout"] == ["2026-09-23"]

    def test_the_occupancy_survives(self):
        q = _query(_public_rate_url(METASEARCH))
        assert q["group_adults"] == ["2"]
        assert q["no_rooms"] == ["1"]

    def test_the_price_basis_survives(self):
        q = _query(_public_rate_url(METASEARCH))
        assert q["sb_price_type"] == ["total"]
        assert q["type"] == ["total"]

    def test_every_other_parameter_survives(self):
        before, after = _query(METASEARCH), _query(_public_rate_url(METASEARCH))
        assert set(before) - set(after) == {"aid", "label"}
        assert all(after[k] == before[k] for k in after)


class TestAUrlWithoutOneIsLeftAlone:
    """Returned unchanged, not merely equivalent: a re-encoded query string
    would rewrite every stored URL on the first fetch after this shipped."""

    PLAIN = ("https://www.booking.com/hotel/in/yelagiri.en-gb.html"
             "?checkin=2026-09-22&checkout=2026-09-23&room1=A%2CA")

    def test_it_is_the_same_string(self):
        assert _public_rate_url(self.PLAIN) is self.PLAIN

    def test_a_url_with_no_query_at_all_is_the_same_string(self):
        bare = "https://www.booking.com/hotel/in/yelagiri.en-gb.html"
        assert _public_rate_url(bare) is bare


class TestItDoesNotCareWhichSiteOrCase:
    def test_another_site_is_cleaned_too(self):
        """Not Booking.com-only. A campaign parameter is a campaign parameter,
        and the next site to price by channel should not need a patch."""
        url = "https://www.example-hotels.com/book?aid=99&checkin=2026-09-22"
        assert "aid" not in _query(_public_rate_url(url))

    def test_an_upper_case_parameter_is_caught(self):
        url = "https://www.booking.com/x.html?AID=1288252&LABEL=metagha&checkin=2026-09-22"
        q = _query(_public_rate_url(url))
        assert "AID" not in q and "LABEL" not in q
        assert q["checkin"] == ["2026-09-22"]
