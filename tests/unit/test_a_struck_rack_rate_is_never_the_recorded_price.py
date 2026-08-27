"""A crossed-out price is not the price.

THE INCIDENT
============
MGM Whispering Meadows was recorded at INR 3,000 on a night it was selling the
same room for INR 2,550. Every room, every reading, exactly 15% high -- and
invisible from the outside, because 3,000 is a perfectly plausible rate and the
fetches all succeeded.

The card renders both numbers, struck one first::

    <span class="discountpirce d-block">INR 3,000.00</span>   <- line-through
    <span id="price ft-14">INR 2,550.00</span>                <- the real one

Note the site's own class name is a typo -- ``discountpirce`` -- and it labels
the ORIGINAL rate rather than the discounted one. Nothing keyed on that class
could work. What is reliable is the computed style: the rack rate resolves to
``text-decoration-line: line-through`` and the sell price to ``none``.

WHY THE EXISTING DEFENCE DID NOT FIRE
=====================================
``dom_discovery`` already penalises struck candidates heavily when it CHOOSES a
selector. But that judgement was made once and then discarded: the selector it
wrote for this site was the generic price pattern, ``text=/(?:INR|Rs)[0-9.,]*/``,
which matches BOTH numbers. ``query_selector`` returns the first, and the first
is the one with the line through it.

So the rule has to hold when the selector is READ, not only when it is written
-- otherwise any selector broad enough to match two prices brings the bug back,
and every already-written config stays broken until someone re-discovers it.
"""
from __future__ import annotations

import pytest

from app.adapters.playwright_direct_site import _price_text_in


class _Element:
    """The parts of a Playwright element handle this code path touches."""

    def __init__(self, text: str, struck: bool = False):
        self._text, self._struck = text, struck

    def inner_text(self) -> str:
        return self._text

    def evaluate(self, _js: str) -> bool:
        return self._struck


class _Card:
    def __init__(self, matches: list[_Element]):
        self._matches = matches

    def query_selector_all(self, _selector: str) -> list[_Element]:
        return self._matches


#: The real MGM card, in the order the DOM presents it.
MGM = [_Element("INR 3,000.00", struck=True), _Element("INR 2,550.00")]


class TestTheStruckPriceIsSkipped:
    def test_the_sell_price_is_returned_not_the_rack_rate(self):
        assert _price_text_in(_Card(MGM), ".price") == "INR 2,550.00"

    @pytest.mark.parametrize(
        "rack,sell",
        [("INR 3,000.00", "INR 2,550.00"),
         ("INR 4,000.00", "INR 3,400.00"),
         ("INR 5,000.00", "INR 4,250.00"),
         ("INR 5,700.00", "INR 4,845.00")],
    )
    def test_every_mgm_room_reads_the_discounted_rate(self, rack, sell):
        card = _Card([_Element(rack, struck=True), _Element(sell)])
        assert _price_text_in(card, ".price") == sell

    def test_several_struck_prices_are_all_skipped(self):
        card = _Card([
            _Element("INR 9,999", struck=True),
            _Element("INR 8,888", struck=True),
            _Element("INR 1,200"),
        ])
        assert _price_text_in(card, ".price") == "INR 1,200"


class TestTheUndiscountedCaseIsUnchanged:
    """The overwhelmingly common card has one price and must be untouched."""

    def test_a_single_price_is_returned(self):
        assert _price_text_in(_Card([_Element("INR 2,550.00")]), ".p") == "INR 2,550.00"

    def test_the_first_price_still_wins_when_none_are_struck(self):
        card = _Card([_Element("INR 2,550.00"), _Element("INR 300.00")])
        assert _price_text_in(card, ".p") == "INR 2,550.00"


class TestDegradingSafely:
    def test_a_price_is_still_returned_when_everything_is_struck(self):
        """A page that crosses out its only price is doing something we do not
        understand, and a price is better evidence than silence."""
        card = _Card([_Element("INR 3,000.00", struck=True)])
        assert _price_text_in(card, ".p") == "INR 3,000.00"

    def test_no_matches_yields_nothing(self):
        assert _price_text_in(_Card([]), ".p") is None

    def test_no_selector_yields_nothing(self):
        assert _price_text_in(_Card(MGM), None) is None

    def test_an_unreadable_style_does_not_count_as_struck(self):
        """A detached node throws when its style is read. Treating that as
        "struck" would skip the only real price on the card."""
        from playwright.sync_api import Error as PlaywrightError

        class _Unreadable(_Element):
            def evaluate(self, _js):
                raise PlaywrightError("node detached")

        assert _price_text_in(_Card([_Unreadable("INR 2,550.00")]), ".p") == "INR 2,550.00"

    def test_a_struck_element_with_empty_text_is_not_returned_as_blank(self):
        card = _Card([_Element("", struck=True), _Element("INR 2,550.00")])
        assert _price_text_in(card, ".p") == "INR 2,550.00"
