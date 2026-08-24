"""A page whose currency is an ICON and whose rack rate is struck through.

Three independent faults met on one real booking page -- RV Resorts Yercaud on
bookingsmaker.com -- and each one alone was enough to report "no prices found"
for a page showing five rates, every one of them discounted:

1. THE CURRENCY IS A CSS CLASS, NOT A CHARACTER
   Font Awesome's rupee glyph is painted by a ::before rule:

       <label class="fa fa-inr">2000</label>

   so the DOM text is bare digits and nothing on the page carries a currency
   character. Every currency test was written against text, so the guard that
   refuses to learn from a priceless page refused this one.

2. textContent GLUES THE TWO PRICES TOGETHER
   The rack rate and the real rate are adjacent elements separated by a <br>,
   so the card's textContent reads "20001300" -- one eight-digit run, which the
   word-anchored bare-number branch of PRICE cannot match at either end. Every
   candidate container therefore parsed as holding no price.

3. THE STRUCK RATE WINS THE TIE
   Both numbers scored zero -- no currency in their text, no rate-ish class --
   and the tie-break prefers the LARGER, on the reasoning that an extra bed is
   never dearer than the bed. The struck-out 2000 beat the real 1300. The
   crossing-out is done with an inline style, and the scan only ever read class
   and id tokens, so it could not see it.

Each assertion below fails on its own if one of the three regresses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.discovery import (
    _candidate_from_dom,
    _corroborate,
    icon_marked_prices,
    why_the_page_cannot_be_learned,
)
from app.adapters.dom_discovery import find_room_cards

# Reduced from the real page, keeping every feature that mattered.
PAGE = """
<html><body>
  <div class="row">
    <div class="entry"><article class="entry-content"><div class="row">
      <div class="col-sm-4"><h2 class="post-title">Deluxe Room</h2></div>
      <div class="col-sm-4"><h4>Room Rate</h4>
        <span><span class="higlight  value">
          <label class="fa fa-inr" style="text-decoration:line-through;">2000</label><br>
          <b><label class="fa fa-inr"></label><label class=" higlight  value">1300</label></b>/Night
        </span></span>
      </div>
    </div></article></div>
    <div class="entry"><article class="entry-content"><div class="row">
      <div class="col-sm-4"><h2 class="post-title">Family Room with Balcony</h2></div>
      <div class="col-sm-4"><h4>Room Rate</h4>
        <span><span class="higlight  value">
          <label class="fa fa-inr" style="text-decoration:line-through;">4450</label><br>
          <b><label class="fa fa-inr"></label><label class=" higlight  value">2892</label></b>/Night
        </span></span>
      </div>
    </div></article></div>
    <div class="entry"><article class="entry-content"><div class="row">
      <div class="col-sm-4"><h2 class="post-title">Deluxe suite with Bathtub</h2></div>
      <div class="col-sm-4"><h4>Room Rate</h4>
        <span><span class="higlight  value">
          <label class="fa fa-inr" style="text-decoration:line-through;">4700</label><br>
          <b><label class="fa fa-inr"></label><label class=" higlight  value">3055</label></b>/Night
        </span></span>
      </div>
    </div></article></div>
  </div>
</body></html>
"""

BODY_TEXT = (
    "Deluxe Room Room Rate 2000 1300 /Night "
    "Family Room with Balcony Room Rate 4450 2892 /Night "
    "Deluxe suite with Bathtub Room Rate 4700 3055 /Night"
)


def test_the_guard_refuses_this_page_when_told_nothing_about_icons():
    """Without the DOM signal the text alone genuinely looks priceless.

    Pinned so the icon exemption cannot quietly widen into "any page with no
    currency is fine", which is the check that stopped a room size being stored
    as a room rate.
    """
    assert why_the_page_cannot_be_learned(BODY_TEXT) is not None


def test_the_guard_allows_it_once_the_currency_is_known_to_be_an_icon():
    assert why_the_page_cannot_be_learned(BODY_TEXT, currency_is_an_icon=True) is None


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
                    pytest.fail(
                        "no room list found on a page showing three rates -- the "
                        "icon currency or the glued textContent has regressed"
                    )
                return cards[0]
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_all_three_rooms_are_found(best):
    assert best["names"] == [
        "Deluxe Room", "Family Room with Balcony", "Deluxe suite with Bathtub",
    ]


def test_it_reads_the_price_a_guest_pays_not_the_struck_out_one(best):
    """The whole point. 2000/4450/4700 are crossed out on the page."""
    assert best["prices"] == [1300, 2892, 3055]
    for struck in (2000, 4450, 4700):
        assert struck not in best["prices"], (
            f"{struck} is struck through on the page and must never be recorded"
        )


# ── the same three faults, against the page they were found on ──────────
#
# The reduced markup above is what each fault looks like in isolation. This is
# the 717-line page as saved, and it is here because a reduction only tests
# what its author already understood: the real page carries four other rooms,
# a booking form, a date picker, twenty-nine "fa fa-user" occupancy icons and
# a "Room Size" line -- every one of them a chance for the scan to prefer
# something that is not a rate.
REAL_PAGE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "probe"
    / "https-www-bookingsmaker-com-ibe-rooms-php-ghotelid-4594-gind.html"
)


@pytest.fixture(scope="module")
def real_page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        pytest.skip("playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.goto(REAL_PAGE.as_uri(), wait_until="load")
                text = page.inner_text("body", timeout=5_000)
                marked = icon_marked_prices(page)
                cards = find_room_cards(page)
                candidate = None
                for card in cards:
                    considered = _candidate_from_dom(card, "https://www.example/rooms")
                    if considered is None:
                        continue
                    _corroborate(considered, text, icon_marked=marked)
                    if considered.is_verified:
                        candidate = considered
                        break
                return {"text": text, "marked": marked, "candidate": candidate}
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_the_real_page_yields_a_verified_room_list(real_page):
    assert real_page["candidate"] is not None, (
        "the saved bookingsmaker page shows five rates and none were confirmed"
    )
    assert real_page["candidate"].room_count == 5


def test_the_real_page_yields_the_discounted_rates(real_page):
    assert [int(p) for p in real_page["candidate"].sample_prices] == [
        1300, 2892, 2892, 2892, 3055,
    ]


def test_an_icon_currency_can_be_repaired_unattended(real_page):
    """The bar for OVERWRITING a live configuration, not merely proposing one.

    ``is_strongly_verified`` asks that at least one price was printed with a
    currency beside it -- the check that stops "Room Size 134 m2" being learned
    as a rate. Asked of the TEXT alone it is unanswerable here, because this
    page never writes a currency character: the room list was discoverable by a
    person and permanently unrepairable by the system, which is the state a
    hotel is left in exactly when nobody is watching.
    """
    candidate = real_page["candidate"]
    assert candidate.corroborated_marked == len(candidate.sample_prices)
    assert candidate.is_strongly_verified is True


def test_the_icon_reader_finds_both_halves_of_a_discounted_pair(real_page):
    """Struck rate included: this set decides what is MONEY, not which price wins.

    Choosing between 2000 and 1300 is the scan's job and is tested above. If
    this set were narrowed to "the price a guest pays" the two judgements would
    be made in two places, and the day they disagreed the rate would corroborate
    against nothing and the repair would silently decline.
    """
    assert real_page["marked"] >= {"1300", "2000", "2892", "3055", "4450", "4700"}


def test_the_page_text_alone_still_looks_priceless(real_page):
    """Not a quirk of the reduction: the real page carries no currency either."""
    assert why_the_page_cannot_be_learned(real_page["text"]) is not None
