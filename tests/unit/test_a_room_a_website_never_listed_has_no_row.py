"""A room a website has never listed gets no "held" row in that website's section.

Every mapped room used to get a row on every website, held with "not on sale
tonight: the site showed no price for this room". For ASG's "Luxury villa" --
a room only Goibibo lists -- that sentence stood on Booking.com and Agoda
every night (18 Nov 2026), reading as something broken. A room that sold out
tonight keeps its row; one the site never sold is named under the table.
"""
from __future__ import annotations

from decimal import Decimal

from app.services import repricing_data as data

BOOKING, AGODA, GOIBIBO = 2, 6, 7
FAMILY_SUITE, SUPERIOR_KING, SUITE_ROOM, LUXURY_VILLA = 29, 28, 94, 98
NAMES = {FAMILY_SUITE: "Family Suite", SUPERIOR_KING: "Superior King Room",
         SUITE_ROOM: "Suite Room", LUXURY_VILLA: "Luxury villa"}
MAPPED = {k: NAMES[k] for k in (FAMILY_SUITE, SUPERIOR_KING, LUXURY_VILLA)}
LISTED = {(BOOKING, FAMILY_SUITE), (BOOKING, SUPERIOR_KING), (AGODA, SUITE_ROOM),
          (AGODA, SUPERIOR_KING), (GOIBIBO, LUXURY_VILLA), (GOIBIBO, SUITE_ROOM)}


def _lane(source_id, ours=None, copies_main=()):
    return data.Lane(channel="x", source_id=source_id, main=False, auto=False,
                     undercut=Decimal("100"), with_tax=True, meal_plan=None,
                     ours=ours or {}, copies_main=frozenset(copies_main))


def test_a_room_only_another_website_lists_is_left_out_and_named():
    kept, never = data.split_never_listed(MAPPED, _lane(BOOKING), LISTED, NAMES)
    assert set(kept) == {FAMILY_SUITE, SUPERIOR_KING}
    assert never == ["Luxury villa"]


def test_a_room_the_site_has_listed_before_keeps_its_row():
    # Superior King is not on Booking.com tonight, but has been: sold out, not absent.
    kept, _ = data.split_never_listed({SUPERIOR_KING: NAMES[SUPERIOR_KING]}, _lane(BOOKING), LISTED, NAMES)
    assert SUPERIOR_KING in kept


def test_a_room_read_under_the_sites_own_name_counts_as_listed():
    # Agoda sells our Family Suite as "Suite Room"; it has no series of its own there.
    bare, never = data.split_never_listed(MAPPED, _lane(AGODA), LISTED, NAMES)
    assert FAMILY_SUITE not in bare and "Family Suite" in never
    kept, never = data.split_never_listed(
        MAPPED, _lane(AGODA, ours={FAMILY_SUITE: "suite  room"}), LISTED, NAMES)
    assert FAMILY_SUITE in kept and never == ["Luxury villa"]


def test_a_room_that_copies_the_main_website_keeps_its_row():
    kept, never = data.split_never_listed(
        MAPPED, _lane(BOOKING, copies_main={LUXURY_VILLA}), LISTED, NAMES)
    assert LUXURY_VILLA in kept and never == []


def test_with_no_website_chosen_nothing_is_left_out():
    kept, never = data.split_never_listed(MAPPED, _lane(None), LISTED, NAMES)
    assert kept == MAPPED and never == []
