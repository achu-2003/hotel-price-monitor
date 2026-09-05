"""Cards that matched but held no room are not proof of a redesign.

THE INCIDENT THIS FILE IS ABOUT
===============================
Treebo Premium Emerald Dove sold out for the night being monitored. Its page
said so where a guest reads it first -- "SOLD OUT for the selected dates", next
to a "Try Different Dates" button -- and the monitor filed, every half hour:

    8 room cards matched but none could be read. The price selector
    'div.jaYzsn' is stale: Room card carried no name.

Two separate faults in one line.

The first is the alert existing at all. DOM extraction can end with no prices
in three ways, and two of them -- nothing configured, and nothing matched --
stop and read the page before blaming anyone, each with a long comment above it
explaining why. The third, "cards matched but none parsed", went straight to
the accusation. It is the branch a sold-out night lands in whenever the stored
room_card is a class that layout divs also carry, which is what discovery
writes on a site built out of styled-components: 'div.jaYzsn' and its siblings
are generated names, they matched eight wrappers, and none of them held a room
name because there were no rooms to hold.

The cost compounds. A drift error is a repair trigger, so each alert spent an
auto-rediscovery attempt on a page with nothing on it to learn; the night went
unrecorded, when "this hotel was full" is the fact worth keeping; and after
eleven failures the circuit breaker opened the source, so the hotel stopped
being checked at all.

The second fault is the sentence. It blamed the price selector and then quoted
a failure about the NAME selector -- because the blame was computed from all
the reasons and the quote was always reasons[0]. Whoever read it was sent to
the half of the config that was working.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.adapters.base import FetchContext
from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter
from app.core.errors import SchemaDriftError
from app.services.dates import StayWindow

#: What the real page rendered around the notice, abbreviated. The header,
#: breadcrumbs, ratings and amenity list a guest scrolls past.
CHROME = (
    "Treebo Premium Emerald Dove with Swimming Pool Sat, 5 Sep - Sun, 6 Sep "
    "1 Room, 2 Adults search Sign In Home Hotels In Yelagiri Hotels In "
    "Kottaiyur location_on Kottaiyur, Yelagiri View on map share 4.5 Very "
    "Good 401 ratings Couple Friendly Pay Later "
)
THE_PANEL = "SOLD OUT for the selected dates View Similar Hotels TRY DIFFERENT DATES"

CONTEXT = FetchContext(
    hotel_source_id=5,
    hotel_name="TREEBO PREMIMUM EMERALD DOVEWITH SWIMMING POOL",
    url="https://www.treebo.com/hotels-in-yelagiri/treebo-premium-emerald-dove-4198/",
    external_id="4198",
    stay=StayWindow(date(2026, 9, 5), date(2026, 9, 6)),
    adults=2,
    children=0,
    currency="INR",
)

#: The shape a repair leaves behind on a styled-components site: generated
#: class names, correct on the night they were learned.
CONFIG = {
    "room_card": "div.jaYzsn",
    "selectors": {"room_name": "div.inMrU", "price": "div.bechBx"},
    "sold_out_markers": ["no rooms available", "sold out", "fully booked"],
}


class _Element:
    def __init__(self, text: str = ""):
        self._text = text

    def inner_text(self) -> str:
        return self._text

    def evaluate(self, _js):  # the struck-price check
        return False


class _Card:
    """One matched element. ``name`` None is a card with no room in it."""

    def __init__(self, name: str | None = None, price: str | None = None, text: str = ""):
        self._name, self._price, self._text = name, price, text

    def query_selector(self, selector):
        if selector == "div.inMrU" and self._name is not None:
            return _Element(self._name)
        return None

    def query_selector_all(self, selector):
        if selector == "div.bechBx" and self._price is not None:
            return [_Element(self._price)]
        return []

    def inner_text(self) -> str:
        return self._text


class _Page:
    def __init__(self, cards: list[_Card], body: str):
        self._cards, self._body = cards, body

    def query_selector_all(self, _selector):
        return self._cards

    def eval_on_selector_all(self, _selector, _js):
        return list(range(len(self._cards)))  # none nested inside another

    def inner_text(self, _selector, timeout=None) -> str:  # noqa: ARG002
        return self._body


class _Fetch:
    def __init__(self, page: _Page):
        self.page = page
        self.json_responses: list = []


def extract(cards: list[_Card], body: str):
    return PlaywrightDirectSiteAdapter()._extract_dom(
        _Fetch(_Page(cards, body)), CONFIG, CONTEXT
    )


class TestTheNightTheHotelWasFull:
    """Eight layout divs matched a selector meant for room cards, on a page
    that says in words there is nothing to sell."""

    PAGE = CHROME + THE_PANEL

    def test_it_is_read_as_sold_out(self):
        offers, sold_out = extract([_Card() for _ in range(8)], self.PAGE)

        assert sold_out is True
        assert offers == []

    def test_it_does_not_raise_a_redesign_alert(self):
        """The alert that fired every half hour, spent a repair attempt each
        time, and finally opened the circuit breaker on the source."""
        extract([_Card() for _ in range(8)], self.PAGE)  # would raise SchemaDriftError

    def test_the_engine_profile_markers_reach_it_too(self):
        """A phrase that comes from the Treebo profile rather than the
        built-in list must count here as it does at the other two exits."""
        offers, sold_out = extract(
            [_Card() for _ in range(8)], CHROME + "no rooms available for these dates"
        )

        assert sold_out is True and offers == []

    def test_a_card_that_lost_only_its_price_counts_the_same(self):
        """The other way this branch is reached: the name still resolves, the
        price does not. On a sold-out page that is not drift either."""
        cards = [_Card(name="Deluxe Room (Maple)", text="Deluxe Room (Maple)") for _ in range(3)]

        offers, sold_out = extract(cards, self.PAGE)

        assert sold_out is True and offers == []


class TestAPageThatReallyHasBeenRedesigned:
    """The alert must still fire. Asking the page is only safe while the other
    answer stays reachable."""

    LISTING = CHROME + "Deluxe Room 1,754 Superior Room 2,100 Book Now"

    def test_it_raises_schema_drift(self):
        with pytest.raises(SchemaDriftError):
            extract([_Card() for _ in range(8)], self.LISTING)

    def test_the_message_says_how_much_text_was_searched(self):
        """Same as the sibling branch: a marker the list is simply missing
        must not look identical to markup that really moved."""
        with pytest.raises(SchemaDriftError) as caught:
            extract([_Card() for _ in range(8)], self.LISTING)

        assert "characters of text the page rendered" in str(caught.value)

    def test_an_empty_page_is_still_drift(self):
        """No text at all is not a claim of sold out."""
        with pytest.raises(SchemaDriftError):
            extract([_Card() for _ in range(8)], "")


class TestTheSentenceNamesOneSelectorAndDescribesIt:
    LISTING = CHROME + "Deluxe Room 1,754 Book Now"

    def test_every_card_nameless_blames_the_name_selector(self):
        with pytest.raises(SchemaDriftError) as caught:
            extract([_Card() for _ in range(3)], self.LISTING)

        message = str(caught.value)
        assert "room_name selector 'div.inMrU'" in message
        assert "carried no name" in message

    def test_a_mixed_page_does_not_blame_one_and_quote_the_other(self):
        """Three cards lost their name, five lost their price. The old message
        blamed the price selector -- and then quoted "Room card carried no
        name" as the evidence for it."""
        cards = [_Card() for _ in range(3)] + [
            _Card(name="Deluxe Room (Maple)", text="Deluxe Room (Maple) Book Now")
            for _ in range(5)
        ]

        with pytest.raises(SchemaDriftError) as caught:
            extract(cards, self.LISTING)

        message = str(caught.value)
        assert "price selector 'div.bechBx'" in message
        assert "carried no name" not in message
