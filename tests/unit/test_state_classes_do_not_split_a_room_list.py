"""A row's decorations must not become part of its shape.

THE FAILURE THIS EXISTS TO STOP
===============================
Booking.com's rate table, for a real Yelagiri property, renders eleven rows
that are the same shape wearing different classes::

    tr.js-rt-block-row.e2e-hprt-table-row                              x5
    tr.js-rt-block-row.e2e-hprt-table-row.hprt-table-last-row          x5
    tr.js-rt-block-row.e2e-hprt-table-row.hprt-table-cheapest-block    x1

``last-row`` and ``cheapest-block`` say where a row sits and what it costs
relative to its neighbours. They say nothing about what a row IS. Signed by
every class it happened to carry, one repeating row became three signatures,
each group blind to the other rooms, and the group of ONE won:

    dom_scan best=tr.js-rt-block-row.e2e-hprt-table-row.hprt-table-cheapest-block
    room_count: 1

An eleven-room hotel monitored as its cheapest room -- and monitored happily,
because the one price it did read was genuinely on the page, so corroboration
passed and every check afterwards reported success. Nothing downstream could
notice the ten rooms that were never mentioned.

The ranking was not at fault. It sorts by how many rooms a candidate can tell
apart, and would have chosen an eleven-room candidate immediately. It was never
offered one.

WHY NOT A LIST OF STATE-CLASS NAMES
===================================
Refusing "cheapest", "last-row", "--selected" and the rest only ever covers the
sites already seen; the next engine names its decorations something else. The
page answers the question itself: a class describing the shape is on every
sibling of that kind, a class describing state is on some of them. So the fix
counts each class across the element's same-tag siblings and keeps the most
widely shared -- which needs to know nothing about any particular site.
"""
from __future__ import annotations

import pytest

from app.adapters.dom_discovery import find_room_cards

# Five rooms, three class combinations, distinct names and prices. Exactly the
# shape of the bug: the plain rows and the "last" rows are equally numerous, so
# a scan that trusts every class finds two rooms at best and one at worst --
# never five.
PAGE = """
<html><body>
  <h1>Availability</h1>
  <div class="rooms-table">
    <div class="rt-row e2e-row">
      <span class="rt-name">Standard Double Room</span>
      <span class="rt-price">&#8377;3,000</span>
    </div>
    <div class="rt-row e2e-row last-row">
      <span class="rt-name">Deluxe Double Room</span>
      <span class="rt-price">&#8377;4,000</span>
    </div>
    <div class="rt-row e2e-row">
      <span class="rt-name">Junior Suite</span>
      <span class="rt-price">&#8377;5,000</span>
    </div>
    <div class="rt-row e2e-row last-row">
      <span class="rt-name">Family Room</span>
      <span class="rt-price">&#8377;6,000</span>
    </div>
    <div class="rt-row e2e-row cheapest-block cheapest-block-fix">
      <span class="rt-name">Private Suite</span>
      <span class="rt-price">&#8377;2,000</span>
    </div>
  </div>
</body></html>
"""

EXPECTED_ROOMS = 5


@pytest.fixture(scope="module")
def scanned():
    """(best candidate, the names its card selector actually reaches)."""
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
                    pytest.fail("the scan found no room list on a five-room page")
                best = cards[0]

                # Read back the way playwright_direct_site will, so this tests
                # what the adapter gets rather than what the scan believes.
                names = []
                for card in page.query_selector_all(best["card"]):
                    hit = card.query_selector(best["name_selector"])
                    if hit:
                        names.append(hit.inner_text().strip())
                return best, names
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_the_card_selector_reaches_every_room(scanned):
    """Five rows in, five rows out. This is the whole bug."""
    _, names = scanned
    assert len(names) == EXPECTED_ROOMS, (
        f"the card selector reached {len(names)} of {EXPECTED_ROOMS} rooms: {names}"
    )


def test_every_room_is_named_distinctly(scanned):
    """Reaching five rows is worthless if they collapse to one name downstream.

    A room's identity is its name, so five cards sharing a name are one room
    with four duplicates -- the same hotel under-monitored by another route.
    """
    _, names = scanned
    assert len(set(names)) == EXPECTED_ROOMS, f"names repeat: {names}"
    assert "Private Suite" in names, (
        "the cheapest row is missing -- it is the one the old scan kept, and "
        "the one most likely to be dropped by an over-correction"
    )


def test_no_state_class_survives_into_the_selector(scanned):
    """The stored selector must describe a row, not a row's mood.

    Asserted on the selector and not only on the count because a selector that
    happens to match five rows today for the wrong reason will stop matching
    the moment the site moves its "cheapest" badge.
    """
    best, _ = scanned
    for state in ("last-row", "cheapest-block", "cheapest-block-fix"):
        assert state not in best["card"], (
            f"{state!r} describes one row's state, not the shape of a row, "
            f"and was stored as part of it: {best['card']}"
        )


def test_the_shape_classes_do_survive(scanned):
    """Narrowing must not strip a selector back to its bare tag.

    "div" alone matches every container on the page. The classes shared by all
    five siblings are exactly what makes this a row rather than a wrapper, and
    dropping them would trade one wrong answer for another.
    """
    best, _ = scanned
    assert "rt-row" in best["card"], (
        f"the class every row shares was dropped: {best['card']}"
    )
