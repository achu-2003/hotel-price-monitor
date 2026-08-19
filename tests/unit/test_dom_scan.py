"""The DOM scanner, against saved copies of real booking pages.

WHY THESE EXIST
===============
Every bug this file has had was silent. The scanner reported "no room list"
and the page looked unreadable, when in fact:

* a stay date in the card meant the first number found was 2026, which is
  correctly refused as a year -- and the whole card was then treated as having
  no price at all;
* "Rs" and "3,200.00" sit in sibling elements, so requiring the currency marker
  in the same leaf rejected all 43 prices on the page;
* the unmarked-number pattern could not span a thousands separator, so
  "3,200.00" parsed as 200 -- wrong by a factor of sixteen, in range, and
  verifiable against the page, because 200 really is printed on it.

None of those raised. Each just produced a confident, wrong, or empty answer,
and the only way to notice was to paste a URL and watch it fail. So the pages
are pinned here as fixtures: no network, no clock, and a failure names the
specific assumption that broke.

The fixtures are saved by ``scripts/probe_site.py``. A browser is needed to
build a DOM at all -- these are DOM heuristics -- so the module skips itself
where Chromium is unavailable rather than failing the suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.dom_discovery import find_room_cards

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"
IPMS247 = FIXTURES / "https-live-ipms247-com-booking-book-rooms-hotelkumararrajapa.html"

pytestmark = pytest.mark.skipif(
    not IPMS247.exists(), reason="probe fixture not present"
)


@pytest.fixture(scope="module")
def scan():
    """Room cards found in the saved eZee/ipms247 booking page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        pytest.skip("playwright is not installed")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(IPMS247.read_text(encoding="utf-8", errors="replace"))
                return find_room_cards(page)
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


class TestRealBookingPage:
    def test_it_finds_a_room_list_at_all(self, scan):
        assert scan, (
            "No candidate on a page that lists three bookable rooms. The scan "
            "is failing before selection, not choosing badly."
        )

    def test_every_room_type_is_found_not_just_the_first(self, scan):
        """Three room types are on this page and all three have to arrive.

        A scan that returns one room looks like it works and quietly monitors a
        third of the hotel.
        """
        best = scan[0]
        assert best["matched"] >= 3, f"only {best['matched']} room(s): {best['names']}"

    def test_the_names_are_room_names_and_not_the_labels_beside_them(self, scan):
        """"Room Rates Exclusive of Tax", "Per Room Per Night" and "1 Room" all
        contain a room word and sit in the same card as the price."""
        names = [n.lower() for n in scan[0]["names"]]
        assert names, "no names extracted"
        for name in names:
            assert "per room per night" not in name
            assert "exclusive of tax" not in name
            assert not name.startswith("1 room")
        assert any("room" in n for n in names)

    def test_prices_are_whole_not_the_part_after_the_comma(self, scan):
        """The rates here are thousands: 3,200.00 and up.

        Reading "3,200.00" as 200 was the failure this pins. Anything under a
        thousand means the separator ate the leading digits again.
        """
        prices = scan[0]["prices"]
        assert prices, "no prices extracted"
        assert all(p >= 1000 for p in prices), f"suspiciously small: {prices}"

    def test_a_date_in_the_card_does_not_hide_the_price(self, scan):
        """These cards carry the stay date, so 2026 is the first number in
        them. Refusing the year must not refuse the card."""
        assert scan[0]["count"] >= 3

    def test_the_card_selector_is_not_a_generated_class(self, scan):
        """styled-components and emotion class names rotate on every deploy.

        Storing one produces a source that works the day it is added and
        silently matches nothing later -- a stale price that still looks live.
        """
        card = scan[0]["card"]
        assert not any(
            part.startswith(("sc-", "css-", "jss", "makeStyles"))
            for part in card.replace("#", ".").split(".")
        ), card


class TestSelectorsAreUsable:
    def test_each_candidate_carries_both_selectors(self, scan):
        for candidate in scan:
            assert candidate["name_selector"]
            assert candidate["price_selector"]

    def test_selectors_are_specific_enough_to_re_read(self, scan):
        """A bare tag name would match half the document."""
        for candidate in scan:
            for key in ("name_selector", "price_selector"):
                value = candidate[key]
                assert (
                    "." in value or "[" in value or "#" in value
                    or value.startswith("text=")
                ), f"{key}={value!r} is not selective"

    def test_no_selector_carries_a_stray_control_character(self, scan):
        r"""A regex written as ``\b`` in the wrong kind of string becomes a
        literal backspace, and the filter it belonged to silently matches
        nothing. That is exactly how the label filters here died."""
        for candidate in scan:
            for key in ("name_selector", "price_selector"):
                assert not any(
                    ord(ch) < 32 for ch in candidate[key]
                ), f"{key}={candidate[key]!r}"
