"""What the system does with a booking page that has no prices on it.

THE INCIDENT THIS FILE IS ABOUT
===============================
HOTEL HILLS TIRUPATTUR sold out for the night being monitored. All three of
its room types rendered

    <span id="vrmprice_span_..." style="display: none;"></span>
    <div class="vrmprice"><p class="rm_sold rmnotavail">Not Available</p>

and the page carried, in 206 KB of HTML, not one price. Two things then went
wrong in sequence, and the second is much the worse of them:

1. The adapter reported "none of the room cards yielded a price, the price
   selector is stale" -- a redesign alert, against a page that had not been
   redesigned, repeating every thirty minutes for as long as the hotel stayed
   full.

2. Auto-repair, newly reachable, ran discovery against that page. With no
   currency anywhere on it, the scan's own marked-price guard switched off,
   bare numbers became admissible, and "Room Size 134 m2" supplied one. It
   stored the "Filter Your Search" sidebar as the room list --

       room_card   div.vres-check-gro          (the amenity checkbox group)
       room_name   div.vres-chk-box > span     (a checkbox label)
       price       div.vres-chk-box > span     (the same checkbox label)

   -- noted "1 rooms, 1/1 prices confirmed against the page", and replaced the
   configuration the monitor was running on. Corroboration agreed, because 134
   genuinely is printed on that page.

The hotel could not then recover on its own: the config that would have read
it once it had rooms to sell had been overwritten by one that never could.

The tests below are the four independent places that failure is now stopped,
each of which is sufficient on its own. The saved page is the real one.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.base import FetchContext
from app.adapters.discovery import (
    Candidate,
    _candidate_from_dom,
    _corroborate,
    why_the_page_cannot_be_learned,
)
from app.adapters.parsing import card_looks_sold_out, looks_sold_out
from app.core.errors import SchemaDriftError
from app.services.dates import StayWindow

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"
SOLD_OUT = FIXTURES / (
    "https-live-ipms247-com-booking-book-rooms-hotelhillstirupattur-soldout.html"
)


# -- 1. discovery refuses to learn from a page with nothing to learn --

class TestAPageWithNoPricesTeachesNothing:
    """The guard that would have stopped the incident at step 2."""

    def test_the_real_sold_out_page_is_refused(self):
        """Verbatim visible text from the page the repair learned from."""
        text = (
            "HOTEL HILLS TIRUPATTUR Home Hotel Info Login "
            "21-08-2026 22-08-2026 Check Availability "
            "Filter Your Search Compare Rooms Rates are in INR ( Rs ) "
            "Air conditioning Show Only Available Rooms "
            "Premium Room with Breakfast Included Room Capacity : 3 2 "
            "Room Rates Inclusive of Tax Room Size 41 m2 , 1 large double bed "
            "Not Available Suite with Breakfast Included "
            "Room Size 134 m2 , 1 extra-large double bed & 1 sofa bed "
            "Not Available Booking Summary No Room(s) Selected"
        )
        assert why_the_page_cannot_be_learned(text) is not None

    def test_the_same_page_with_its_rates_showing_is_learnable(self):
        """The refusal must lift the moment the hotel has a room to sell."""
        text = (
            "HOTEL HILLS TIRUPATTUR Filter Your Search "
            "Rates are in INR ( Rs ) Air conditioning "
            "Premium Room with Breakfast Included Room Size 41 m2 "
            "Rs 3,200.00 Suite with Breakfast Included Room Size 134 m2 "
            "Rs 4,381.00"
        )
        assert why_the_page_cannot_be_learned(text) is None

    def test_a_currency_split_across_elements_still_counts(self):
        """eZee renders "<p>Rs</p><span>3,200.00</span>"; inner_text joins the
        two with a newline, and that must not read as an unmarked number."""
        assert why_the_page_cannot_be_learned("Deluxe Room\nRs\n3,200.00") is None

    def test_a_page_that_says_sold_out_is_refused_even_with_a_price_on_it(self):
        """A struck-through "was Rs 4,000" beside "Sold Out" is not a rate."""
        assert why_the_page_cannot_be_learned("Deluxe Room Rs 4,000 Sold Out")

    def test_an_empty_page_is_refused(self):
        assert why_the_page_cannot_be_learned("") is not None

    def test_the_reason_is_specific_enough_to_act_on(self):
        """It goes in front of a person, so "no" is not a sufficient answer."""
        reason = why_the_page_cannot_be_learned("Premium Room Not Available")
        assert reason and "price" in reason


# -- 2. a bare number is not a corroborated price ---------------------

class TestCorroborationCanBeFooledAndTheStrongerTestCannot:
    """Why ``is_verified`` alone could not have caught this."""

    PAGE = (
        "Premium Room Room Size 41 m2 Rs 3,200.00 "
        "Suite Room Size 134 m2 Not Available"
    )

    def _candidate(self, name: str, price: str) -> Candidate:
        found = Candidate(
            source_url="https://example.test/rooms",
            rooms_path=".card",
            fields={"room_name": ".name", "price": ".price"},
            kind="dom",
            sample_names=[name],
            sample_prices=[Decimal(price)],
        )
        _corroborate(found, self.PAGE)
        return found

    def test_a_room_size_passes_the_old_bar(self):
        """Not a regression -- the documented behaviour that turned out to be
        insufficient. 134 is on the page, so "134 is a price" corroborates."""
        assert self._candidate("Suite", "134").is_verified

    def test_a_room_size_fails_the_bar_a_repair_answers_to(self):
        assert not self._candidate("Suite", "134").is_strongly_verified

    def test_a_real_rate_clears_both(self):
        found = self._candidate("Premium Room", "3200")
        assert found.is_verified and found.is_strongly_verified

    def test_decimals_and_separators_do_not_break_the_match(self):
        """The page writes "Rs 3,200.00"; a payload writes 3200.0."""
        assert self._candidate("Premium Room", "3200.0").corroborated_marked == 1

    def test_a_price_absent_from_the_page_clears_neither(self):
        found = self._candidate("Invented", "7777")
        assert not found.is_verified and not found.is_strongly_verified


# -- 3. one element is never both the room and its rate ---------------

class TestNameAndPriceMayNotBeTheSameSelector:
    """The exact shape the repair wrote into the database."""

    def _card(self, name_selector: str, price_selector: str) -> dict:
        return {
            "card": "div.vres-check-gro",
            "name_selector": name_selector,
            "price_selector": price_selector,
            "names": ["Air conditioning"],
            "prices": [134],
            "count": 4,
            "matched": 1,
        }

    def test_the_stored_config_would_now_be_refused(self):
        assert _candidate_from_dom(
            self._card("div.vres-chk-box > span", "div.vres-chk-box > span"),
            "https://live.ipms247.com/booking/book-rooms-hotelhillstirupattur",
        ) is None

    def test_two_different_selectors_are_still_accepted(self):
        assert _candidate_from_dom(
            self._card(".room-name", ".room-price"), "https://example.test"
        ) is not None

    @pytest.mark.parametrize("missing", ["name_selector", "price_selector"])
    def test_a_missing_selector_is_refused_rather_than_stored_empty(self, missing):
        card = self._card(".room-name", ".room-price")
        card[missing] = ""
        assert _candidate_from_dom(card, "https://example.test") is None


# -- 4. a sold-out room reads as sold out, not as drift ---------------

class _Node:
    """The two methods the adapter actually calls on a DOM element."""

    def __init__(self, text: str, children: dict | None = None):
        self._text = text
        self._children = children or {}

    def inner_text(self) -> str:
        return self._text

    def query_selector(self, selector: str):
        return self._children.get(selector)


SELECTORS = {"room_name": ".name", "price": ".price"}


def _offer(full_text: str, *, name: str, price: str):
    from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter

    card = _Node(full_text, {".name": _Node(name), ".price": _Node(price)})
    return PlaywrightDirectSiteAdapter()._offer_from_card(
        card,
        SELECTORS,
        FetchContext(
            hotel_source_id=20,
            hotel_name="HOTEL HILLS TIRUPATTUR",
            url="https://live.ipms247.com/booking/book-rooms-hotelhillstirupattur",
            external_id=None,
            stay=StayWindow(date(2026, 8, 21), date(2026, 8, 22)),
            adults=2,
            children=0,
            currency="INR",
        ),
    )


class TestASoldOutCard:
    """Card text verbatim from the page; the price element empty, as it
    renders when the room is not for sale."""

    TEXT = (
        "Premium Room with Breakfast Included Room Capacity : 3 2 "
        "Room Rates Inclusive of Tax Room Size 41 m2 , 1 large double bed "
        "Not Available Room Info Enquire Availability Calendar"
    )

    def _built(self):
        return _offer(
            self.TEXT, name="Premium Room with Breakfast Included", price=""
        )

    def test_it_does_not_raise_schema_drift(self):
        """This is the false redesign alert that fired every thirty minutes
        for a hotel that was simply full."""
        assert self._built() is not None

    def test_it_is_recorded_as_unavailable(self):
        assert self._built().is_available is False

    def test_no_price_is_invented_for_it(self):
        offer = self._built()
        assert offer.price_inclusive is None and offer.price_exclusive is None


class TestTheSafeguardsOnThatLeniency:
    """"Not available" is admissible inside a card under two conditions, and
    these are the tests that fail if either one is dropped."""

    def test_a_bookable_room_saying_breakfast_is_not_available_keeps_its_price(self):
        offer = _offer(
            "Deluxe Room Rs 3,200.00 Breakfast not available on this rate",
            name="Deluxe Room",
            price="Rs 3,200.00",
        )
        assert offer.is_available is True
        assert offer.price_inclusive == Decimal("3200")

    def test_a_priceless_card_with_no_explanation_is_still_drift(self):
        """The genuine stale-selector case has to survive intact."""
        with pytest.raises(SchemaDriftError):
            _offer(
                "Deluxe Room 2 Adults 1 Room",
                name="Deluxe Room",
                price="Room Info",
            )

    def test_a_price_outside_the_plausible_range_is_still_drift(self):
        """"1 extra-large double bed" read as a one-rupee room is exactly what
        the bounds exist to refuse. The sold-out path must not smuggle it
        through by relaxing them -- which is what the non-raising parser,
        whose floor is zero, would have done."""
        with pytest.raises(SchemaDriftError):
            _offer(
                "Suite 1 extra-large double bed and 1 sofa bed",
                name="Suite",
                price="1 extra-large double bed",
            )


class TestWhereEachSoldOutTestMayBeApplied:
    """The page-wide test and the card-scoped one are not interchangeable."""

    LEGEND = "Available  Not Available"

    def test_the_page_wide_test_ignores_the_legend_every_ezee_page_prints(self):
        """That legend is on the page whether the hotel is full or empty.
        Reading it as an announcement would report a page that had merely
        failed to load its rates as a hotel with no rooms, and tell whoever
        watches that hotel so."""
        assert looks_sold_out(self.LEGEND) is False

    def test_the_card_scoped_test_reads_it(self):
        """Safe only because nothing wider than one card is ever passed in."""
        assert card_looks_sold_out("Premium Room Not Available") is True

    def test_the_card_scoped_test_still_agrees_about_real_announcements(self):
        assert card_looks_sold_out("Sold Out") is True


# -- the page itself, through the real scan ---------------------------

@pytest.mark.skipif(not SOLD_OUT.exists(), reason="probe fixture not present")
class TestTheScanAgainstTheRealSoldOutPage:
    """Saved from the failing check run, 21 Aug 2026 10:32 IST."""

    @pytest.fixture(scope="class")
    def scanned(self):
        """``(candidates, page_text)`` from the real page in a real browser."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - environment without playwright
            pytest.skip("playwright is not installed")

        from app.adapters.dom_discovery import find_room_cards

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
                try:
                    page = browser.new_page()
                    page.set_content(
                        SOLD_OUT.read_text(encoding="utf-8", errors="replace")
                    )
                    return find_room_cards(page), page.inner_text("body")
                finally:
                    browser.close()
        except Exception as exc:  # pragma: no cover - no browser binary
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")

    def test_the_page_is_refused_before_the_scan_is_even_consulted(self, scanned):
        assert why_the_page_cannot_be_learned(scanned[1]) is not None

    def test_no_candidate_reads_one_element_as_both_name_and_price(self, scanned):
        offenders = [
            card["card"]
            for card in scanned[0]
            if card.get("name_selector") == card.get("price_selector")
        ]
        assert not offenders, (
            f"The scan still offers a candidate whose room name and its price "
            f"are the same element: {offenders}. That is the shape that was "
            f"stored for this hotel."
        )

    def test_nothing_the_scan_returns_could_be_written_by_a_repair(self, scanned):
        """Belt and braces. Even reaching past the guard that refuses this
        page outright, no candidate found on it may reach the database."""
        cards, page_text = scanned
        writable = []
        for card in cards:
            found = _candidate_from_dom(card, "https://example.test")
            if found is None:
                continue
            _corroborate(found, page_text)
            if found.is_strongly_verified:
                writable.append(found.fields)
        assert not writable


# -- 5. an unreadable page must not cost the repair budget ------------

class TestTheAttemptBudgetIsSpentOnFailuresNotOnEmptyNights:
    """The budget rations browser runs against a site that will not yield.
    A hotel with no rooms to sell tonight is not that site."""

    def _state(self, attempts: int):
        from app.services.rediscovery import RepairState

        return RepairState(attempts=attempts)

    def _attempts(self, fragment: dict) -> int:
        from app.services.rediscovery import STATE_KEY

        return fragment[STATE_KEY]["attempts"]

    def test_a_refund_hands_the_claimed_attempt_back(self):
        from datetime import UTC, datetime

        settled = self._state(2).settle(
            datetime(2026, 8, 21, tzinfo=UTC), outcome="unlearnable", refund=True
        )
        assert self._attempts(settled) == 1

    def test_it_cannot_go_below_zero(self):
        from datetime import UTC, datetime

        settled = self._state(0).settle(
            datetime(2026, 8, 21, tzinfo=UTC), outcome="unlearnable", refund=True
        )
        assert self._attempts(settled) == 0

    def test_an_ordinary_failure_still_costs_one(self):
        from datetime import UTC, datetime

        settled = self._state(2).settle(
            datetime(2026, 8, 21, tzinfo=UTC), outcome="unverified"
        )
        assert self._attempts(settled) == 2

    def test_a_successful_repair_still_clears_the_slate(self):
        from datetime import UTC, datetime

        settled = self._state(2).settle(
            datetime(2026, 8, 21, tzinfo=UTC), outcome="repaired", reset=True
        )
        assert self._attempts(settled) == 0

    def test_three_sold_out_nights_do_not_exhaust_the_budget(self):
        """The sequence that would otherwise retire a working hotel."""
        from datetime import UTC, datetime

        from app.services.rediscovery import RepairState, may_attempt

        state = RepairState()
        now = datetime(2026, 8, 21, tzinfo=UTC)
        for _ in range(3):
            claimed = RepairState(attempts=state.claim(now)["auto_repair"]["attempts"])
            state = RepairState(
                attempts=self._attempts(
                    claimed.settle(now, outcome="unlearnable", refund=True)
                )
            )
        assert may_attempt(
            state, now=now, enabled=True, cooldown_minutes=0, max_attempts=3
        ).allowed


# -- 6. the whole path, on the real page ------------------------------

@pytest.mark.skipif(not SOLD_OUT.exists(), reason="probe fixture not present")
class TestTheAdapterAgainstTheRealSoldOutPage:
    """The check run that started this, replayed end to end.

    Selectors are the ones this page actually needs -- ``div.vrmprice`` is
    where the rate goes when there is one -- so this is the adapter reading a
    correctly configured hotel that happens to have nothing for sale. Before
    the change it raised SchemaDriftError: three rooms, no prices, therefore a
    redesign. It is not a redesign. It is a full hotel.
    """

    CONFIG = {
        "room_card": "div.vres-card.vres-roomlisting",
        "selectors": {"price": "div.vrmprice", "room_name": "h3"},
    }

    @pytest.fixture(scope="class")
    def extracted(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - environment without playwright
            pytest.skip("playwright is not installed")

        from app.adapters.playwright_base import BrowserFetch
        from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter

        context = FetchContext(
            hotel_source_id=20,
            hotel_name="HOTEL HILLS TIRUPATTUR",
            url="https://live.ipms247.com/booking/book-rooms-hotelhillstirupattur",
            external_id=None,
            stay=StayWindow(date(2026, 8, 21), date(2026, 8, 22)),
            adults=2,
            children=0,
            currency="INR",
        )
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
                try:
                    page = browser.new_page()
                    page.set_content(
                        SOLD_OUT.read_text(encoding="utf-8", errors="replace")
                    )
                    return PlaywrightDirectSiteAdapter()._extract_dom(
                        BrowserFetch(page=page, json_responses=[]),
                        self.CONFIG,
                        context,
                    )
                finally:
                    browser.close()
        except Exception as exc:  # pragma: no cover - no browser binary
            if isinstance(exc, SchemaDriftError):
                raise
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")

    def test_all_three_rooms_are_read(self, extracted):
        offers, _ = extracted
        assert [o.raw_room_name for o in offers] == [
            "Premium Room", "Suite", "Executive Room"
        ]

    def test_every_one_of_them_is_unavailable(self, extracted):
        offers, _ = extracted
        assert not any(offer.is_available for offer in offers)

    def test_not_one_price_is_invented(self, extracted):
        offers, _ = extracted
        assert all(
            offer.price_inclusive is None and offer.price_exclusive is None
            for offer in offers
        )

    def test_the_fetch_reports_the_hotel_as_sold_out(self, extracted):
        assert extracted[1] is True
