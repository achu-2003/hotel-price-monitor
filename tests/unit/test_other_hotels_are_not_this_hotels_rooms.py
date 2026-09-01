"""A "similar properties" carousel is not a room list.

THE FAILURE THIS EXISTS TO STOP
===============================
Treebo shows one room card and hides the rest behind "View All Rooms". Further
down the same page sits a carousel of neighbouring hotels -- repeated cards,
one price each, four distinct names. To every measure the ranking has, that
carousel is the better room list, and it won::

    a.gjOMp        4 names, 4 prices  ->  "Treebo Premium Emerald Dove...",
                                          "Itsy Hotels Kurinji Stay Inn...",
                                          "Treebo SNS Grand Inn...",
                                          "Treebo Laa Gardenia Resort..."
    div.inMrU...   1 name,  1 price   ->  "Deluxe Room (Maple)"

Four to one. Stored, that monitors four COMPETITORS' cards under this hotel's
name, and it verifies perfectly -- those prices really are on the page, so
corroboration passes and every check afterwards reports success. Nothing
downstream can catch it: a price from the hotel next door is a real price.

The carousel loaded below the fold, which is the only reason the configs in
production were right: the run that made them never scrolled that far. A
re-detect on any of those hotels would have replaced a correct config with
this one.

WHAT SEPARATES THEM
===================
Not the count, not the names, not the prices -- by all three the carousel wins.
Where a click goes. Each carousel card is wrapped in a link to a DIFFERENT
property page; a room card does not navigate away from the hotel it belongs
to. Booking.com's room rows link to "#RD680595401", within the page.
Cleartrip's link nowhere at all.

Two conditions, both required, because "contains a link" alone would demote
real room lists on any engine that gives each room a Book link:

  * every card leads somewhere off this page, and
  * each one leads somewhere DIFFERENT -- four hotels are four destinations,
    while four Book links are four ways to reach one checkout.

And it sorts rather than rejects, so a site whose room cards genuinely are
links still works when it is the only candidate there is.
"""
from __future__ import annotations

import pytest

from app.adapters.dom_discovery import find_room_cards

# Absolute hrefs on purpose: set_content serves from about:blank, against which
# a relative URL cannot be resolved, and the rule would never fire.
OTHER = "https://example.test/hotels/"

PAGE = f"""
<html><body>
  <h2>Available Rooms for Your Stay</h2>
  <div id="rooms">
    <div class="rt-card">
      <h3 class="rt-name">Deluxe Room (Maple)</h3>
      <div class="rt-price">&#8377; 4,129</div>
    </div>
  </div>

  <h2>Similar properties</h2>
  <div class="reco">
    <a class="reco-card" href="{OTHER}emerald-dove">
      <h3 class="reco-name">Emerald Dove Premium Resort</h3>
      <div class="reco-price">&#8377; 5,632</div>
    </a>
    <a class="reco-card" href="{OTHER}kurinji-stay">
      <h3 class="reco-name">Kurinji Stay Villa</h3>
      <div class="reco-price">&#8377; 5,294</div>
    </a>
    <a class="reco-card" href="{OTHER}sns-grand">
      <h3 class="reco-name">SNS Grand Inn Suite</h3>
      <div class="reco-price">&#8377; 3,366</div>
    </a>
    <a class="reco-card" href="{OTHER}laa-gardenia">
      <h3 class="reco-name">Laa Gardenia Premium Stay</h3>
      <div class="reco-price">&#8377; 6,676</div>
    </a>
  </div>
</body></html>
"""

NEIGHBOURS = ["Emerald Dove", "Kurinji", "SNS Grand", "Laa Gardenia"]


@pytest.fixture(scope="module")
def best():
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
                    pytest.fail("the scan found nothing on a page with a room and a carousel")
                return cards[0]
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_the_one_real_room_beats_four_other_hotels(best):
    """One room outranks a carousel that outnumbers it four to one."""
    assert best["names"] == ["Deluxe Room (Maple)"], best["names"]


def test_no_neighbouring_hotel_is_stored_as_a_room(best):
    """Said separately because this is the damage, not the symptom.

    A competitor's price recorded under this hotel is indistinguishable from a
    correct reading once it is in the database.
    """
    joined = " ".join(best["names"]).lower()
    for neighbour in NEIGHBOURS:
        assert neighbour.lower() not in joined, best["names"]


def test_the_chosen_card_does_not_navigate_away(best):
    """The flag itself, so a future change to the sort cannot quietly undo it."""
    assert best["linksAway"] == 0, best["card"]


def test_a_room_card_with_one_instance_is_never_flagged(best):
    """A single card cannot demonstrate a pattern, and must not be judged as if
    it had. The room list here has exactly one card -- the common shape on a
    page that hides the rest behind "View All Rooms" -- and flagging it would
    leave the carousel as the only candidate standing."""
    assert best["matched"] == 1, best["matched"]
    assert best["linksAway"] == 0
