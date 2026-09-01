"""A repeated heading loses to a heading that varies -- but only to a heading.

THE FAILURE THIS EXISTS TO STOP
===============================
Cleartrip nests rate plans inside room types, and renders both as an ``h4``::

    h4.sc-fqkvVR.hFFAkE                            "Deluxe Room - Pool view"
    h4.sc-fqkvVR.bPeojd.room--inclusions--header   "Room with Breakfast & Dinner"

The second is longer, so it scored higher; it is a heading, so it was trusted;
and a trusted name was returned the moment it was found. Nine room types were
stored as "Room with Breakfast & Dinner" -- one identity, nine prices. Eight
offers collided with the ninth and were dropped, and the hotel was monitored as
one room. The fetch said so, in as many words::

    8 of 9 offers shared an identity with another offer in the same fetch AT A
    DIFFERENT PRICE, so the cheaper or dearer of each pair was dropped and this
    hotel is being monitored as 1 room(s).

TWO THINGS HAD TO CHANGE, AND A THIRD HAD TO NOT
================================================
1. The only candidate whose cards held the room titles was thrown away before
   it was ranked. Its signature, ``div.iWfHoM.component-stacked-slots``, is a
   layout component Cleartrip also uses for the page header -- and the header
   was first in document order, had a price (the sticky book bar) and no room
   name, so the group was rejected on that one reading. The group now looks for
   a node that yields both rather than trusting whichever came first.

2. Nine cards reading one name ranked as nine rooms, because a trusted name was
   allowed its full card count. It beat the room titles, which found seven
   names in eight cards. A selector that distinguished nothing now scores what
   it can distinguish.

3. And the thing that must NOT change: ``RATE_PLANS`` in test_dom_scan.py is
   one room type on three rate plans, where "Deluxe Room" repeats and the board
   basis varies. Preferring whatever varies would name those rooms "Room Only"
   and "With Breakfast". So a trusted name is displaced only by another TRUSTED
   name -- on Cleartrip both are headings; there the alternative is a bare div.
"""
from __future__ import annotations

import pytest

from app.adapters.dom_discovery import find_room_cards

# The shape of the bug. The sticky price bar comes first and has no room name,
# the rate-plan heading outscores every room title by being longer, and both
# the title and the plan are h4 -- so nothing but "does it vary" separates them.
PAGE = """
<html><body>
  <div class="wrap">
    <div class="stacked"><h5 class="amt">&#8377; 8,995</h5></div>
    <div class="stacked">
      <h4 class="title">Deluxe Room</h4>
      <div class="rate">
        <h4 class="plan">Room with Breakfast &amp; Dinner</h4>
        <h5 class="amt">&#8377; 8,995</h5>
      </div>
    </div>
    <div class="stacked">
      <h4 class="title">Garden Villa</h4>
      <div class="rate">
        <h4 class="plan">Room with Breakfast &amp; Dinner</h4>
        <h5 class="amt">&#8377; 9,495</h5>
      </div>
    </div>
    <div class="stacked">
      <h4 class="title">Palace Suite</h4>
      <div class="rate">
        <h4 class="plan">Room with Breakfast &amp; Dinner</h4>
        <h5 class="amt">&#8377; 10,495</h5>
      </div>
    </div>
  </div>
</body></html>
"""

ROOMS = ["Deluxe Room", "Garden Villa", "Palace Suite"]


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
                    pytest.fail("the scan found no room list on a three-room page")
                return cards[0]
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_the_rooms_are_named_after_the_room(best):
    """Three room types, three names. The whole bug in one assertion."""
    assert sorted(best["names"]) == ROOMS, best["names"]


def test_the_rate_plan_never_becomes_the_room_name(best):
    """Stated separately because it is the symptom an operator would see.

    Every name identical is what collapses nine offers onto one identity, and
    it reads on the dashboard as a hotel with a single room.
    """
    for name in best["names"]:
        assert "breakfast" not in name.lower(), best["names"]
        assert "dinner" not in name.lower(), best["names"]
    assert len(set(best["names"])) == len(ROOMS), (
        f"names collapsed to {len(set(best['names']))} distinct: {best['names']}"
    )


def test_a_group_survives_an_unrepresentative_first_node(best):
    """The sticky price bar is first, has a price, and names no room.

    Reading only that one node rejected the entire group -- the only group
    whose cards contained the room titles -- so the rooms were never ranked at
    all. Reaching three named rooms proves the group was not thrown away on it.
    """
    assert best["matched"] == len(ROOMS), (
        f"matched {best['matched']} cards, expected {len(ROOMS)}"
    )


def test_a_selector_that_names_nothing_cannot_outrank_one_that_names(best):
    """The rate-plan heading matches MORE cards than the title does.

    It reads on all three rate boxes and, being trusted, was credited with
    three rooms for one name -- which is how it won. Ranking now asks what a
    candidate can tell apart, so `distinct` decides.
    """
    assert best["distinct"] == len(ROOMS), best["names"]
    assert best["name_selector"] != "h4.plan", (
        "the rate-plan heading was stored as the room name"
    )
