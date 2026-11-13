"""One room's automatic repricing can be switched off on one website.

13 Nov 2026: the owner wanted the automatic runs to stop moving a room while
the others kept following the market.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import repricing as rule


def test_the_rooms_switched_off_are_read_per_website():
    settings = SimpleNamespace(auto_off_rooms={"Booking.com": [3, "5"], "Goibibo": [7]})
    assert rule.room_auto_off(settings, "Booking.com") == {3, 5}
    assert rule.room_auto_off(settings, "Goibibo") == {7}
    assert rule.room_auto_off(settings, "Agoda") == frozenset()


def test_nothing_saved_or_unreadable_means_every_room_moves():
    assert rule.room_auto_off(SimpleNamespace(auto_off_rooms=None), "Booking.com") == frozenset()
    assert rule.room_auto_off(SimpleNamespace(), "Booking.com") == frozenset()
    assert rule.room_auto_off(SimpleNamespace(auto_off_rooms={"Booking.com": ["x"]}),
                              "Booking.com") == frozenset()

