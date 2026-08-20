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


BOOKMYSTAY = (
    """<!doctype html>
<html><body><div id="main-content"><div class="mainpage">
  <h1>Zeenath Taj Gardens</h1>
  <p>Choose a room to continue your booking.</p>
  <div class="roomNewContainer">
    <div class="roomName"><h2>Standard</h2></div>
    <p class="recommended">Recommended</p>
    <div class="room__midContainer"><b>⁨₹⁩ 2,017</b><span>/</span><span>Night</span>
      <span>Plus Taxes</span></div>
    <div><span>2 Guests 1 Room</span><button>View Room</button><button>Select Room</button></div>
  </div>
  <div class="roomNewContainer">
    <div class="roomName"><h2>Super Deluxe Room</h2></div>
    <p class="showMoreAmenities">+13 More</p>
    <div class="room__midContainer"><b>⁨₹⁩ 4,381</b><span>/</span><span>Night</span>
      <span>Plus Taxes</span></div>
    <div><span>2 Guests 1 Room</span><button>View Room</button><button>Select Room</button></div>
  </div>
</div></div></body></html>"""
)


class TestPageThatHidesItsCurrencySymbol:
    """A real property whose ₹ is wrapped in Unicode directional isolates.

    The site renders "<isolate>₹<isolate> 2,017". The symbol is then not
    adjacent to the digits, so the marked-price branch cannot match and 2,017
    is left looking like a bare number -- inside the year range, where bare
    numbers are refused. One room vanished from a two-room hotel, and the
    survivor was the one whose price happens not to look like a year.

    Everything here is one page: the scan must find both rooms, name them from
    the page rather than from its buttons, and -- the part that actually broke
    in production -- emit selectors that still work when Playwright applies
    them to the untouched DOM.
    """

    @pytest.fixture(scope="class")
    def page_and_scan(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            pytest.skip("playwright is not installed")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
                try:
                    page = browser.new_page()
                    page.set_content(BOOKMYSTAY)
                    cards = find_room_cards(page)
                    # Resolve the emitted selectors while the page is still open.
                    resolved = []
                    if cards:
                        best = cards[0]
                        for card in page.query_selector_all(best["card"]):
                            name_el = card.query_selector(best["name_selector"])
                            price_el = card.query_selector(best["price_selector"])
                            resolved.append((
                                (name_el.inner_text().strip() if name_el else None),
                                (price_el.inner_text().strip() if price_el else None),
                            ))
                    return cards, resolved
                finally:
                    browser.close()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")

    def test_both_rooms_are_found(self, page_and_scan):
        cards, _ = page_and_scan
        assert cards, "no candidate at all"
        assert cards[0]["matched"] == 2, cards[0]["names"]

    def test_the_cheaper_room_is_not_mistaken_for_a_year(self, page_and_scan):
        cards, _ = page_and_scan
        assert 2017 in cards[0]["prices"], cards[0]["prices"]
        assert 4381 in cards[0]["prices"], cards[0]["prices"]

    def test_rooms_are_named_from_the_page_not_from_its_buttons(self, page_and_scan):
        """Every card contains "View Room" and "Select Room"."""
        cards, _ = page_and_scan
        assert sorted(cards[0]["names"]) == ["Standard", "Super Deluxe Room"]

    def test_the_card_is_the_room_not_the_whole_page(self, page_and_scan):
        """#main-content holds both rooms, so it yields exactly one "room"."""
        cards, _ = page_and_scan
        assert cards[0]["card"] == "div.roomNewContainer"

    def test_the_emitted_selectors_work_on_the_untouched_dom(self, page_and_scan):
        """The scan reads text with invisible characters stripped; Playwright,
        applying the stored selectors later, does not. An anchored price
        pattern matched every card and then no price inside any of them."""
        _, resolved = page_and_scan
        assert len(resolved) == 2
        for name, price in resolved:
            assert name, f"name selector resolved to nothing (got {resolved!r})"
            assert price, f"price selector resolved to nothing (got {resolved!r})"
        assert [n for n, _ in resolved] == ["Standard", "Super Deluxe Room"]

    def test_the_name_selector_is_not_built_from_one_rooms_name(self, page_and_scan):
        """"text=/Standard/i" matches the first card and nothing after it."""
        cards, _ = page_and_scan
        assert "Standard" not in cards[0]["name_selector"]
        assert "Deluxe" not in cards[0]["name_selector"]


ZOTEL = (
    """<!doctype html>
<html><body><div class="primary-section search-result mx-auto">
  <h1>JP GLAMPING RESORT</h1>
  <div class="hotel-room old col-span-4">
    <h5 class="text-lg font-semibold mb-2">Suite</h5>
    <div class="roomtype-features text-gray-500 text-sm flex flex-wrap gap-1 mb-2">
      <div class="mr-3 mb-0 font-light">King Size Bed</div>
      <div class="mr-3 mb-0 font-light">20.00 Sq.ft</div>
      <div class="facility-item font-light category-3354 mr-3 mb-0">Balcony View</div>
    </div>
    <div class="roomtype-price text-base mb-2">
      <div class="text-gray-900 font-bold"><span class="total-price">&#8377; 3,390</span>
        <span class="text-red-500 font-light line-through text-xs ml-2 total-standard-rate">&#8377; 3,390</span></div>
      <div class="text-gray-500 font-normal text-xs room_type_tax_3354">+ &#8377; 169.5 in taxes and charges</div>
    </div>
    <button>Select Room</button>
  </div>
  <div class="hotel-room old col-span-4">
    <h5 class="text-lg font-semibold mb-2">Deluxe Studio</h5>
    <div class="roomtype-features text-gray-500 text-sm flex flex-wrap gap-1 mb-2">
      <div class="mr-3 mb-0 font-light">King Size Bed</div>
      <div class="mr-3 mb-0 font-light">20.00 Sq.ft</div>
      <div class="facility-item font-light category-3355 mr-3 mb-0">Balcony View</div>
    </div>
    <div class="roomtype-price text-base mb-2">
      <div class="text-gray-900 font-bold"><span class="total-price">&#8377; 8,550</span>
        <span class="text-red-500 font-light line-through text-xs ml-2 total-standard-rate">&#8377; 8,550</span></div>
      <div class="text-gray-500 font-normal text-xs room_type_tax_3355">+ &#8377; 1539 in taxes and charges</div>
    </div>
    <button>Select Room</button>
  </div>
</div></body></html>"""
)


class TestTailwindPageWhereEveryCardSharesALabel:
    """A real six-room property that was monitored as one room for weeks.

    Three independent defects lined up, and none of them raised:

    1. The struck-through-price penalty tested class names as SUBSTRINGS, and
       Tailwind's "font-bold" contains "old". The genuine rate was scored as a
       stale price and lost to the tax line printed beneath it, so the hotel's
       cheapest room was recorded as costing 169.50 -- its own tax.
    2. The amenity chips live in a "roomtype-features" wrapper, which collected
       the same "this holds the name" bonus as the real title, and "King Size
       Bed" outscored the <h5> by being longer.
    3. Reading the same name from all six cards, the pipeline resolved them to
       one room type and dropped five as duplicate offer keys. The check run
       recorded six offers found, zero unmatched, and success.

    Everything a person could see said the fetch worked. Only the room count
    disagreed, and nothing was comparing it to anything.
    """

    @pytest.fixture(scope="class")
    def scan(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            pytest.skip("playwright is not installed")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
                try:
                    page = browser.new_page()
                    page.set_content(ZOTEL)
                    return find_room_cards(page)
                finally:
                    browser.close()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")

    def test_the_rate_is_read_and_not_the_tax_line_beneath_it(self, scan):
        """169.50 is a real number printed on the page, so corroboration
        confirms it as readily as the rate. Only the scoring separates them."""
        prices = scan[0]["prices"]
        assert 3390 in prices and 8550 in prices, prices
        assert 169.5 not in prices and 1539 not in prices, prices

    def test_font_bold_is_not_read_as_an_old_price(self, scan):
        """The whole-word rule. If "font-bold" matches /old/ again, the rate
        is penalised into second place and this returns the tax figure."""
        assert scan[0]["price_selector"] == "span.total-price", scan[0]["price_selector"]

    def test_rooms_are_named_from_the_heading_not_the_amenity_chips(self, scan):
        names = scan[0]["names"]
        assert sorted(names) == ["Deluxe Studio", "Suite"], names
        assert "King Size Bed" not in names, names

    def test_a_name_that_reads_the_same_on_every_card_is_rejected(self, scan):
        """The general guard, and the one that does not depend on any class
        being sensibly named: rooms in a list have DIFFERENT names."""
        best = scan[0]
        assert best["distinct"] == best["matched"] == 2, best

    def test_a_short_real_name_beats_a_longer_shared_label(self, scan):
        """"Suite" is five characters; "King Size Bed" is thirteen and sits in
        a wrapper whose class says "roomtype". Length must not decide it."""
        assert "Suite" in scan[0]["names"], scan[0]["names"]


# No heading anywhere, so the room's name has nothing but a short bare <div>
# to sit in, and a repeated amenity chip outscores it on length alone. This is
# the case the scoring rules CANNOT get right by themselves.
NO_HEADING = (
    """<!doctype html><html><body><div class="results">
  <div class="room-card">
    <div class="rt">Suite</div>
    <div class="roomtype-features"><div class="mr-3 font-light">King Size Bed</div></div>
    <div class="rate"><span class="amount">&#8377; 3,390</span></div>
  </div>
  <div class="room-card">
    <div class="rt">Villa</div>
    <div class="roomtype-features"><div class="mr-3 font-light">King Size Bed</div></div>
    <div class="rate"><span class="amount">&#8377; 5,690</span></div>
  </div>
</div></body></html>"""
)

# One room type, three rate plans. Every card genuinely says "Deluxe Room".
RATE_PLANS = (
    """<!doctype html><html><body><div class="results">
  <div class="room-card">
    <h3 class="room-title">Deluxe Room</h3><div class="board">Room Only</div>
    <div class="rate"><span class="amount">&#8377; 3,200</span></div>
  </div>
  <div class="room-card">
    <h3 class="room-title">Deluxe Room</h3><div class="board">With Breakfast</div>
    <div class="rate"><span class="amount">&#8377; 3,800</span></div>
  </div>
  <div class="room-card">
    <h3 class="room-title">Deluxe Room</h3><div class="board">Half Board</div>
    <div class="rate"><span class="amount">&#8377; 4,500</span></div>
  </div>
</div></body></html>"""
)


def _scan_html(html):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(html)
                return find_room_cards(page)
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


class TestRepeatedNamesAreJudgedBySource:
    """Repetition alone cannot decide whether a name selector is broken.

    Two pages here look identical to any check that only counts distinct names,
    and the right answer is opposite in each. What separates them is WHERE the
    name came from: a heading or a self-declared name container is taken at its
    word, an amenity chip is not.
    """

    @pytest.fixture(scope="class")
    def no_heading(self):
        return _scan_html(NO_HEADING)

    @pytest.fixture(scope="class")
    def rate_plans(self):
        return _scan_html(RATE_PLANS)

    def test_a_repeated_chip_is_replaced_by_the_name_it_hid(self, no_heading):
        """The chip outscores the real name and there is no heading to rescue
        it, so only the cross-card check can catch this."""
        best = no_heading[0]
        assert sorted(best["names"]) == ["Suite", "Villa"], best["names"]
        assert best["name_selector"] == "div.rt", best["name_selector"]

    def test_a_repeated_heading_is_left_alone(self, rate_plans):
        """One room type, three rate plans. Rejecting this named the rooms
        after their board basis -- "Room Only", "With Breakfast"."""
        best = rate_plans[0]
        assert set(best["names"]) == {"Deluxe Room"}, best["names"]
        assert best["name_selector"] == "h3.room-title", best["name_selector"]
        assert best["name_trusted"] is True

    def test_the_board_basis_never_becomes_the_room_name(self, rate_plans):
        for name in rate_plans[0]["names"]:
            assert "breakfast" not in name.lower()
            assert "board" not in name.lower()
            assert "room only" not in name.lower()


class TestVerificationUsesBothFacts:
    """`is_verified` is the last gate before a config is stored."""

    def _candidate(self, names, *, trusted):
        from decimal import Decimal

        from app.adapters.discovery import Candidate

        return Candidate(
            source_url="https://example.test/rooms",
            rooms_path="div.room-card",
            fields={"room_name": "div.x", "price": "span.y"},
            kind="dom",
            sample_names=list(names),
            sample_prices=[Decimal("3200"), Decimal("3800")],
            corroborated=2,
            name_trusted=trusted,
        )

    def test_an_untrusted_repeat_is_refused(self):
        """"King Size Bed" twice, from an amenity chip. The prices are real and
        on the page, so corroboration alone would have stored this."""
        assert not self._candidate(
            ["King Size Bed", "King Size Bed"], trusted=False
        ).is_verified

    def test_a_trusted_repeat_is_accepted(self):
        assert self._candidate(["Deluxe Room", "Deluxe Room"], trusted=True).is_verified

    def test_distinct_names_are_accepted_either_way(self):
        assert self._candidate(["Suite", "Villa"], trusted=False).is_verified

    def test_one_room_is_never_refused_for_failing_to_differ(self):
        assert self._candidate(["Deluxe Room"], trusted=False).is_verified
