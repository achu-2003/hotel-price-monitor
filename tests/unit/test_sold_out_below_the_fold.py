"""A hotel that is full is not a hotel that has been redesigned.

THE INCIDENT THIS FILE IS ABOUT
===============================
Hotels Kurinji Stay Inn sold out for the night being monitored. Its Treebo page
said so, in the panel a guest reads first:

    SOLD OUT for the selected dates
    [View Similar Hotels] [Try Different Dates] [Notify Me]

and the monitor filed, every half hour, a parse_schema_drift alert reading

    No elements matched room_card selector '#t-roomTypes' and the page does
    not say it is sold out. This is almost certainly a redesign.

The page said it twice: once in the built-in marker list, once in the engine
profile's own ``sold_out_markers``. Neither was found, because the decision
read a PREFIX:

    body = _safe_text(page, "body")[:4000]

Treebo puts a header, a search bar, breadcrumbs, the hotel name, its rating,
amenities and the whole policy list ahead of the booking panel. Measured on the
saved artifact from that failure: the page rendered 10,780 characters of text
and "SOLD OUT" began at 8,319. The cut fell four thousand characters short of
the answer.

The cost is not only the alert. A drift alert is a repair trigger, so each one
spent an auto-rediscovery attempt on a page with no rooms on it -- and the
night itself was never recorded as sold out, so the series has a gap where a
business fact belongs.

Eleven lines above the failing check, ``_wait_for_price`` searches the WHOLE
body for the same markers and returns early when it finds one. It had already
found this. The answer was known and then thrown away by a slice.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.adapters.base import FetchContext
from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter
from app.core.errors import SchemaDriftError
from app.services.dates import StayWindow

#: The order the real page renders in, abbreviated but faithful where it
#: matters: everything a guest scrolls past before the booking panel.
BEFORE_THE_PANEL = (
    "Treebo Itsy Hotels Kurinji Stay Inn Tue, 25 Aug - Wed, 26 Aug "
    "1 Room, 2 Adults search Sign In Home Hotels In Yelagiri "
    "Hotels In Athanavoor Itsy Hotels Kurinji Stay Inn with Swimming Pool "
    "share 4.5 Very Good 412 ratings location_on Athanavoor, Yelagiri "
    "View on map Amenities Free WiFi Parking Restaurant Room Service "
    "Power Backup Laundry Doctor on Call Daily Housekeeping "
    "Check-in 12:00 pm Check-out 11:00 am Couple Friendly "
    "This hotel welcomes unmarried couples Read all policies Pay Later "
) * 17  # ~8,300 characters ahead of the panel, as the real page had

THE_PANEL = "SOLD OUT for the selected dates View Similar Hotels Try Different Dates Notify Me"

CONTEXT = FetchContext(
    hotel_source_id=16,
    hotel_name="Hotels Kurinji Stay Inn",
    url="https://www.treebo.com/hotels-in-yelagiri/itsy-hotels-kurinji-stay-inn-3965/",
    external_id="3965",
    stay=StayWindow(date(2026, 8, 25), date(2026, 8, 26)),
    adults=2,
    children=0,
    currency="INR",
)

CONFIG = {
    "room_card": "#t-roomTypes",
    "selectors": {"room_name": "text=/Room \\(/", "price": "text=/^₹\\s?[\\d,]+$/"},
    # Verbatim from the Treebo profile in app/adapters/engines.py.
    "sold_out_markers": ["no rooms available", "sold out", "fully booked"],
}


class _Page:
    """A page with no room cards on it, and a body of the caller's choosing."""

    def __init__(self, body: str):
        self._body = body

    def query_selector_all(self, _selector) -> list:
        return []

    def inner_text(self, _selector, timeout=None) -> str:  # noqa: ARG002
        return self._body


class _Fetch:
    def __init__(self, page: _Page):
        self.page = page
        self.json_responses: list = []


def extract(body: str):
    return PlaywrightDirectSiteAdapter()._extract_dom(_Fetch(_Page(body)), CONFIG, CONTEXT)


class TestTheNoticeThatSitsBelowTheFold:
    PAGE = BEFORE_THE_PANEL + THE_PANEL

    def test_the_fixture_reproduces_the_conditions(self):
        """If the notice moves inside the first 4,000 characters this file
        stops testing anything, and would keep passing while it did."""
        assert self.PAGE.index("SOLD OUT") > 4_000

    def test_it_is_read_as_sold_out(self):
        offers, sold_out = extract(self.PAGE)

        assert sold_out is True
        assert offers == []

    def test_it_does_not_raise_a_redesign_alert(self):
        """The false alert that fired every half hour for as long as the hotel
        stayed full."""
        extract(self.PAGE)  # would raise SchemaDriftError

    def test_the_engine_profile_markers_reach_it_too(self):
        """Both spellings failed for the same reason, so both are pinned. This
        page says only "no rooms available", which is not in the built-in
        list -- it comes from the Treebo profile."""
        offers, sold_out = extract(
            BEFORE_THE_PANEL + "no rooms available for these dates"
        )

        assert sold_out is True and offers == []


class TestAPageThatReallyHasBeenRedesigned:
    """The alert must still fire. Widening the search is only safe if the
    other answer is still reachable."""

    def test_it_raises_schema_drift(self):
        with pytest.raises(SchemaDriftError):
            extract(BEFORE_THE_PANEL + "Deluxe Room ₹1,754 Book Now")

    def test_the_message_says_how_much_text_was_searched(self):
        """The old message asserted a redesign and gave nothing to check that
        claim against, so a marker the list is simply missing looked exactly
        like a site that had moved its markup."""
        with pytest.raises(SchemaDriftError) as caught:
            extract(BEFORE_THE_PANEL + "Deluxe Room ₹1,754 Book Now")

        assert "characters of text the page rendered" in str(caught.value)

    def test_an_empty_page_is_still_drift(self):
        """No text at all is not a claim of sold out."""
        with pytest.raises(SchemaDriftError):
            extract("")
