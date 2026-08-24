"""A page whose room name is nowhere near its price.

THE INCIDENT THIS FILE IS ABOUT
===============================
Kolaahalam Mainland Resorts on swiftbook.io lists SEVEN room types. The monitor
watched two of them, named "Room" and "Villa", and said so on Attention:

    2 of 13 offers shared an identity with another offer in the same fetch and
    were dropped, so this hotel is being monitored as 11 room(s) instead of 13.
    The room_name selector is almost certainly reading a label every room card
    shares.

    selectors: {"price": "div.current-price.fs12.notranslate",
                "room_name": "label.fs12"}

The alert was right about the symptom and the scan could not have avoided it,
because the room name is not reachable from the price. swiftbook renders each
rate inside a collapsible price-breakdown widget, so the distance from the
price element up to the element holding the room's name is EIGHTEEN levels:

    div.current-price                       <- the price
      ... 9 levels of breakdown / card / row ...
    div.col-lg-12.d-flex                    <- the rate row
      ... 7 more levels ...
    div.col-lg-8...sidecard-col-padding     <- the room. h3 with its name.

The ancestor walk stopped at six. No container it could see held both a price
and a room name, so it settled for the rate row and took the only label inside
it -- an occupancy chip reading "Room" or "Villa". Three things then had to be
true at once for that to reach the database, and each is pinned below:

1. THE WALK COULD NOT REACH THE ROOM. Six levels was chosen on the reasoning
   that a card sits close to its price. A price-breakdown widget breaks it.

2. THE RANKING PREFERRED IT. Candidates sorted by how many cards they found
   before how many names they could tell apart, so thirteen rate rows sharing
   two names outranked seven rooms with seven names.

3. THE GUARD LET IT THROUGH. is_verified refused an untrusted name selector
   only when EVERY name was identical. Four "Room"s and two "Villa"s are not
   one name, so it passed.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.discovery import Candidate, _candidate_from_dom
from app.adapters.dom_discovery import find_room_cards

PAGE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "probe"
    / "https-www-swiftbook-io-inst-home-propertyid-362ntrti6w1gsaeh.html"
)

# What the site shows, in the order the page lists them.
EXPECTED_ROOMS = [
    "Standard Double Room",
    "Deluxe Double Room",
    "One-Bedroom Villa",
    "Superior Double Room",
    "Premium Room",
    "Family Suite",
    "Three-Bedroom Villa",
]
EXPECTED_PRICES = [5000, 6500, 7000, 7000, 8300, 13000, 25000]


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
                page = browser.new_page(viewport={"width": 1366, "height": 900})
                page.goto(PAGE.as_uri(), wait_until="load")
                page.wait_for_timeout(1_000)
                cards = find_room_cards(page)
                if not cards:
                    pytest.fail("no room list found on a page showing seven rooms")
                return cards[0]
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


def test_every_room_the_site_lists_is_found(best):
    """Seven, not two. The count is the whole complaint."""
    assert best["names"] == EXPECTED_ROOMS


def test_the_room_name_is_read_not_the_category_chip(best):
    """"Room" and "Villa" are what the rate rows say; they are not room names."""
    assert best["name_selector"] != "label.fs12"
    assert "Room" not in best["names"]  # the bare word, not "Standard Double Room"
    assert "Villa" not in best["names"]


def test_each_room_gets_its_own_price(best):
    """The price the adapter will read back: the first match inside the card.

    Sampled prices used to come from each card's best-scoring price element
    while the adapter reads the first one matching the stored selector. On a
    room card holding a rate per plan those are different elements, and the
    scan reported 6,800 for a room that would be fetched at 5,000.
    """
    assert best["prices"] == EXPECTED_PRICES


def test_the_names_are_all_different(best):
    """What identity downstream is built from. Duplicates here are lost rooms."""
    assert len(set(best["names"])) == len(best["names"])


def test_the_candidate_survives_verification(best):
    candidate = _candidate_from_dom(best, "https://www.swiftbook.io/inst/")
    assert candidate is not None
    assert candidate.room_count == 7
    assert candidate.sample_names == EXPECTED_ROOMS


class TestTheGuardThatShouldHaveCaughtIt:
    """is_verified, against the exact names the broken selector produced.

    Independent of the scan: even if a future change lets a category chip win
    again, this refuses to write it into a live configuration.
    """

    @staticmethod
    def _candidate(names: list[str], *, trusted: bool) -> Candidate:
        return Candidate(
            source_url="https://www.swiftbook.io/inst/",
            rooms_path="div.col-lg-12.col-md-12.d-flex",
            fields={"room_name": "label.fs12", "price": "div.current-price"},
            kind="dom",
            sample_names=names,
            sample_prices=[Decimal(1000 + i) for i in range(len(names))],
            room_count=len(names),
            corroborated=len(names),
            name_trusted=trusted,
        )

    def test_the_category_chip_is_refused(self):
        chips = ["Room", "Room", "Room", "Room", "Villa", "Villa", "Room", "Room"]
        assert self._candidate(chips, trusted=False).is_verified is False

    def test_rooms_with_their_own_names_are_accepted(self):
        assert self._candidate(EXPECTED_ROOMS, trusted=False).is_verified is True

    def test_one_room_type_on_several_rate_plans_is_still_accepted(self):
        """From a HEADING, repetition reads as rate plans and must survive.

        The other silent wrongness: rejecting this names the rooms after their
        board basis instead.
        """
        plans = ["Deluxe Room", "Deluxe Room", "Deluxe Room"]
        assert self._candidate(plans, trusted=True).is_verified is True
        assert self._candidate(plans, trusted=False).is_verified is False
