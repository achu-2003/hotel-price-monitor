"""Selectors the scan emits must work where the ADAPTER runs them.

THE FAILURE THIS EXISTS TO STOP
===============================
A room card holding an unclassed heading::

    <div class="room-card">
      <h2>Standard Room</h2>
      <div class="price">Rs 2,000 <span>/ night</span></div>
    </div>

The scan built the name selector from the heading's wrapper and stored
``div.room-card > h2``. It then proved it worked, three cards out of three,
and recorded "3/3 prices confirmed against the page".

It had proved nothing, because the two engines are not the same one:

    the scan       card.querySelector(sel) -- the browser's own, which matches
                   the selector against the whole DOCUMENT and then keeps the
                   hits under `card`. An ancestor named in the selector may sit
                   outside the card, including BEING the card.
    the adapter    ElementHandle.query_selector(sel) -- Playwright's engine,
                   which evaluates strictly inside the card's subtree, where
                   the card is not a descendant of itself.

So every check afterwards read no name from any card. The hotel sat broken with
auto-repair re-deriving the same dead selector and reporting "no change".

These tests therefore resolve each selector THE WAY THE ADAPTER WILL, through
``ElementHandle.query_selector``. Asserting the selector's shape would not have
caught this -- the shape was reasonable, it just meant something else where it
ran.
"""
from __future__ import annotations

import pytest

from app.adapters.dom_discovery import find_room_cards

PAGE = """
<html><body>
  <h1>Our Hotel Rooms</h1>
  <div class="rooms-container">
    <div class="room-card">
      <div class="room-image">Room Image</div>
      <h2>Standard Room</h2>
      <p>Comfortable room for 2 guests.</p>
      <div class="price">&#8377;2,000 <span>/ night</span></div>
    </div>
    <div class="room-card">
      <div class="room-image">Room Image</div>
      <h2>Deluxe Room</h2>
      <p>Spacious room with premium facilities.</p>
      <div class="price">&#8377;3,000 <span>/ night</span></div>
    </div>
    <div class="room-card">
      <div class="room-image">Room Image</div>
      <h2>Suite Room</h2>
      <p>Luxury room with extra space and facilities.</p>
      <div class="price">&#8377;4,500 <span>/ night</span></div>
    </div>
  </div>
</body></html>
"""


@pytest.fixture(scope="module")
def scanned():
    """(best candidate, every card read back through its own selectors)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        pytest.skip("playwright is not installed")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(PAGE)
                cards = find_room_cards(page)
                if not cards:
                    pytest.fail("the scan found no room list on a three-room page")
                best = cards[0]

                # Exactly what playwright_direct_site does per card.
                read = []
                for card in page.query_selector_all(best["card"]):
                    name_el = card.query_selector(best["name_selector"])
                    price_el = card.query_selector(best["price_selector"])
                    read.append((
                        name_el.inner_text().strip() if name_el else None,
                        price_el.inner_text().strip() if price_el else None,
                    ))
                return best, read
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_the_name_selector_resolves_inside_every_card(scanned):
    best, read = scanned
    names = [name for name, _ in read]
    assert all(names), (
        f"name selector {best['name_selector']!r} resolved in "
        f"{sum(1 for n in names if n)}/{len(names)} cards. A selector rooted at "
        f"the card itself cannot match inside it."
    )


def test_the_name_selector_reads_the_heading_not_the_prose(scanned):
    """The bare-heading case, which used to fall through to a text selector.

    ``text=/Standard/i`` is built from the FIRST card's wording, so it found
    "Standard Room" in card one and, no other card containing that word, the
    paragraph of prose in cards two and three.
    """
    _, read = scanned
    assert [name for name, _ in read] == [
        "Standard Room", "Deluxe Room", "Suite Room",
    ]


def test_the_price_selector_resolves_inside_every_card(scanned):
    best, read = scanned
    prices = [price for _, price in read]
    assert all(prices), f"price selector {best['price_selector']!r} missed a card"
    assert [p.split("/")[0].strip() for p in prices] == [
        "₹2,000", "₹3,000", "₹4,500",
    ]


def test_no_emitted_selector_is_rooted_at_the_card(scanned):
    """The shape check, as a second line of defence behind the behavioural ones.

    Kept narrow on purpose: an INTERMEDIATE wrapper ("div.roomName > h2") is
    both legitimate and necessary. Only the card's own selector is forbidden as
    a prefix, because that is the one an element cannot be a descendant of.
    """
    best, _ = scanned
    card = best["card"]
    for field in ("name_selector", "price_selector"):
        selector = best[field]
        assert not selector.startswith(f"{card} >"), (
            f"{field} is {selector!r}, rooted at the card {card!r} it runs inside"
        )
