"""A room whose name says how many people it sleeps is still a room.

WHAT WENT WRONG
===============
Booking.com sells "Deluxe Double Room (2 Adults + 1 Child)". The scan refused
that text outright, because the small-print filter turned away anything saying
"adults" or "guests" -- words meant to catch the occupancy line BESIDE a room
rather than the name of one.

With the name gone, the only candidates left on the row were the amenity
badges: "Room", "33 m²", "Pool view". The scan settled on the badge, and a
five-room property was monitored as

    Room  5,500 | Room  6,500 | Entire villa  7,650 | Room  13,000 | ...

three of which share one identity at three different prices, so two of every
check were dropped as duplicates. Before that it had settled on the bed
summary and reported rooms called "Bed:", "Bedroom:" and "Beds:".

Nothing failed. The names were on the page, the prices were on the page, and
every verification downstream agreed -- which is why it survived a repair that
reported itself as successful.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.dom_discovery import find_room_cards

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"
ANANTHYAM = FIXTURES / "https-www-booking-com-hotel-in-ananthyam-resort-by-anukulas.html"
ASG = FIXTURES / "https-www-booking-com-hotel-in-ags-holiday-resorts-yelagiri1.html"

pytestmark = pytest.mark.skipif(
    not ANANTHYAM.exists() or not ASG.exists(), reason="probe fixtures not present"
)


def _scan(path: Path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        pytest.skip("playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            try:
                page = browser.new_page()
                page.set_content(path.read_text(encoding="utf-8", errors="replace"))
                return find_room_cards(page)
            finally:
                browser.close()
    except Exception as exc:  # pragma: no cover - no browser binary
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")


@pytest.fixture(scope="module")
def ananthyam():
    return _scan(ANANTHYAM)


@pytest.fixture(scope="module")
def asg():
    return _scan(ASG)


class TestTheRoomWhoseNameCountsItsGuests:
    def test_the_scan_finds_a_room_list(self, ananthyam):
        assert ananthyam, "no candidate at all on a page listing five rooms"

    def test_the_occupancy_in_the_name_does_not_delete_the_name(self, ananthyam):
        best = ananthyam[0]
        assert any("Deluxe Double Room" in n for n in best["names"]), (
            f"the room named after its occupancy is missing: {best['names']}"
        )

    def test_the_rooms_are_named_not_badged(self, ananthyam):
        """"Room", "Entire villa" and "33 m²" are badges beside the name."""
        best = ananthyam[0]
        assert "Room" not in best["names"], (
            f"an amenity badge is being read as a room name: {best['names']}"
        )

    def test_every_room_is_told_apart_from_every_other(self, ananthyam):
        """The failure was not the labels; it was two rooms sharing one.

        Identity downstream is the name, so repeated names at different prices
        are dropped as duplicates and the hotel is watched with rooms missing.
        """
        best = ananthyam[0]
        assert len(set(best["names"])) == len(best["names"]), (
            f"names collide, so offers will be dropped: {best['names']}"
        )

    def test_the_prices_come_with_them(self, ananthyam):
        best = ananthyam[0]
        assert len(best["prices"]) == len(best["names"])
        assert all(p > 0 for p in best["prices"])


class TestTheHotelThatAlreadyWorked:
    """The same engine, read correctly before this change and after it.

    The first attempt at this fix inherited names down table rows, which read
    ASG as ten rows with "Junior Suite" three times -- fixing one property by
    breaking another. This is the guard against doing that again.
    """

    def test_all_five_rooms_are_found(self, asg):
        best = asg[0]
        assert best["matched"] == 5, f"expected 5 rooms, got {best['matched']}"

    def test_they_are_the_real_room_names(self, asg):
        assert set(asg[0]["names"]) == {
            "Junior Suite",
            "Deluxe Double Room",
            "Standard Double Room",
            "Superior King Room",
            "Family Suite",
        }

    def test_no_name_repeats(self, asg):
        names = asg[0]["names"]
        assert len(set(names)) == len(names)
