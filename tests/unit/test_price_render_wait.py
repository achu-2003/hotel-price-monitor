"""Waiting for the rooms to be PRICED, not merely present.

THE ALERT THIS PREVENTS
=======================
One monitored source waits on ``#t-roomTypes`` -- a section that exists as soon
as the page renders, while its rates arrive from an XHR a second or two later.
Reading the moment the container appeared found the card, found no price in it,
and raised

    1 room cards matched but none yielded a price.
    The price selector 'text=/^₹\\s?[\\d,]+$/' is stale.

The selector was not stale. It read ₹1,616 correctly on the very next check,
and on eight of the eleven runs around the three that failed. The alert said
"this site was redesigned, go and rewrite the config" about a config that was
working -- the most expensive kind of wrong, because acting on it means
replacing something correct.

Because the failure is a race, it cannot be pinned with a static page: the
point is the gap between the container appearing and the price appearing. So
these drive a real, deliberately slow-rendering page.
"""
from __future__ import annotations

import pytest

from app.adapters.base import FetchContext
from app.adapters.playwright_base import BrowserFetch
from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter
from app.services.dates import StayWindow

# The container is in the initial HTML. The price is added 1.2s later, which is
# what every booking engine does and what the old wait walked straight past.
LATE_PRICE = """<!doctype html><html><body>
  <div id="t-roomTypes">
    <h3>Deluxe Room (Maple)</h3>
    <span id="slot"></span>
  </div>
  <script>
    setTimeout(function () {
      document.getElementById('slot').innerHTML =
        '<span class="rate">&#8377;1,616</span>';
    }, 1200);
  </script>
</body></html>"""

# Never prices anything, and says why. The wait must not burn its whole budget.
SOLD_OUT = """<!doctype html><html><body>
  <div id="t-roomTypes"><h3>Deluxe Room (Maple)</h3><p>No rooms available</p></div>
</body></html>"""

CONFIG = {
    "room_card": "#t-roomTypes",
    "wait_for": "#t-roomTypes",
    "selectors": {"room_name": "h3", "price": "span.rate"},
    "sold_out_markers": ["no rooms available", "sold out"],
}


def _context() -> FetchContext:
    return FetchContext(
        hotel_source_id=1,
        hotel_name="Hotels Kurinji Stay Inn",
        url="https://example.test/rooms",
        external_id=None,
        stay=StayWindow.__new__(StayWindow),
        adults=2,
    )


@pytest.fixture
def page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                yield browser.new_page()
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


class TestAPriceThatRendersLate:
    def test_the_adapter_waits_for_it(self, page):
        page.set_content(LATE_PRICE)
        adapter = PlaywrightDirectSiteAdapter()

        # Present immediately -- which is exactly the trap.
        assert page.query_selector("#t-roomTypes") is not None
        assert page.query_selector("span.rate") is None

        adapter._wait_for_rooms(BrowserFetch(page=page), CONFIG)

        assert page.query_selector("span.rate") is not None, (
            "returned while the container was empty -- extraction will now "
            "report a working config as a stale selector"
        )

    def test_the_offer_is_then_read_correctly(self, page):
        """The whole point: no drift, and the real price."""
        page.set_content(LATE_PRICE)
        adapter = PlaywrightDirectSiteAdapter()
        fetch = BrowserFetch(page=page)

        adapter._wait_for_rooms(fetch, CONFIG)
        offers, sold_out = adapter._extract_dom(fetch, CONFIG, _context())

        assert not sold_out
        assert len(offers) == 1
        assert offers[0].raw_room_name == "Deluxe Room (Maple)"
        assert str(offers[0].price_inclusive) == "1616"


class TestAPageThatWillNeverPrice:
    def test_a_sold_out_marker_ends_the_wait_early(self, page):
        """Otherwise every sold-out night pays the full price budget, and a
        false alarm has been traded for a slow check."""
        import time

        page.set_content(SOLD_OUT)
        adapter = PlaywrightDirectSiteAdapter()

        started = time.monotonic()
        adapter._wait_for_rooms(BrowserFetch(page=page), CONFIG)
        elapsed = time.monotonic() - started

        assert elapsed < 5, f"waited {elapsed:.1f}s on a page that says sold out"

    def test_the_wait_is_bounded_when_nothing_says_anything(self, page):
        """No price, no marker. It must give up and let extraction judge --
        which is where "sold out" and "redesigned" are told apart."""
        import time

        page.set_content(
            """<!doctype html><html><body>
               <div id="t-roomTypes"><h3>Deluxe Room</h3></div></body></html>"""
        )
        adapter = PlaywrightDirectSiteAdapter()
        config = {**CONFIG, "price_wait_ms": 1000, "sold_out_markers": []}

        started = time.monotonic()
        adapter._wait_for_rooms(BrowserFetch(page=page), config)
        elapsed = time.monotonic() - started

        assert 0.8 < elapsed < 4, f"budget of 1s was not honoured ({elapsed:.1f}s)"


class TestItStillDoesNotMaskARealBreak:
    def test_a_page_with_no_rooms_at_all_is_untouched(self, page):
        """The wait must not swallow the case it exists to distinguish from."""
        page.set_content("<!doctype html><html><body><p>Hello</p></body></html>")
        adapter = PlaywrightDirectSiteAdapter()
        config = {**CONFIG, "wait_timeout_ms": 700, "price_wait_ms": 500}

        # Returns quietly; raising here would report a sold-out weekend as a
        # broken adapter, which is why this step never raises.
        adapter._wait_for_rooms(BrowserFetch(page=page), config)

        from app.core.errors import SchemaDriftError

        with pytest.raises(SchemaDriftError):
            adapter._extract_dom(BrowserFetch(page=page), config, _context())
