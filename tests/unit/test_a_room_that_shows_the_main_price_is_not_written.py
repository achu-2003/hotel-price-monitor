"""A website that shows our Booking.com price for a room is not written to.

RMS decides which rate row each website reads. For ASG, Agoda reads the
Booking.com rows for most rooms (proven 26 Oct 2026: every Agoda price equalled
the Booking.com one to the paisa while the Agoda rows sat untouched). The
Agoda lane still wrote the Agoda row, RMS took it, and the page then said
"waiting for Agoda to show the new price" for ever, because it never would.

The owner now marks such a room "shows our Booking.com price" on that
website. The rule holds it with that reason (page and worker alike, since
both call propose()), and nothing is written to the row the site ignores.

A website that showed no price at all for a mapped room keeps a held row
too, so every mapped room appears in every website's section.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.schemas.repricing import RoomPair
from app.services import repricing as rule
from app.services import repricing_data as data

D = Decimal
NIGHT = date(2026, 11, 11)
OURS = SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS")
STERLING = SimpleNamespace(id=10, name="Sterling")
AGODA = 6
STANDARD, DELUXE = 27, 25
OPEN = rule.Rule(max_step_pct=D("100"), floor_pct=D("90"), ceiling_pct=D("500"), round_to=10)


def _series(room_type_id, excl):
    return SimpleNamespace(
        room_type_id=room_type_id, is_available=True, source_id=AGODA, meal_plan=None,
        last_price_exclusive=D(excl), last_taxes_fees=D("0"), last_price_inclusive=None,
        current_price=None, currency="INR", offer_key=f"x-{room_type_id}",
        check_in=NIGHT, check_out=NIGHT + timedelta(days=1),
    )


def _rows():
    return [
        (_series(STANDARD, "5764"), OURS, "Standard Double Room"),
        (_series(DELUXE, "5163"), OURS, "Deluxe Double Room"),
        (_series(30, "5069"), STERLING, "Mountain View Classic Room"),
        (_series(31, "4548"), STERLING, "Classic Room"),
    ]


def _lane_row(pairs):
    return SimpleNamespace(channel="Agoda", source_id=AGODA, auto_enabled=True,
                           benchmark_undercut=D("100"), benchmark_with_tax=False,
                           benchmark_meal_plan=None, room_pairs=pairs)


def _propose(lane):
    bench = data.lane_benchmark(STERLING, lane, "Booking.com")
    return {p.room_type_id: p for p in rule.propose(
        data.lane_rows(_rows(), OURS.id, lane), own_hotel_id=OURS.id, rule=OPEN,
        room_type_ids={STANDARD, DELUXE}, benchmark=bench)}


PAIRED = {"27": {"theirs": "Mountain View Classic Room"}, "25": {"theirs": "Classic Room"}}


def test_paired_the_agoda_row_is_proposed_as_before():
    p = _propose(data.extra_lane(_lane_row(PAIRED)))
    assert p[STANDARD].target is not None and p[STANDARD].held is None
    assert p[DELUXE].target is not None


def test_a_room_that_shows_the_main_price_is_held_with_the_reason():
    pairs = {**PAIRED, "27": {"copies_main": True}}
    p = _propose(data.extra_lane(_lane_row(pairs)))
    assert p[STANDARD].target is None, "nothing may be written to the row Agoda ignores"
    assert "Booking.com price" in p[STANDARD].held
    assert p[STANDARD].our_price == D("5764"), "our Agoda price is still shown"
    assert p[DELUXE].target is not None, "the other room is untouched"


def test_copying_wins_over_a_leftover_pair():
    """A stored name of theirs left beside the flag is dropped, not half-used."""
    lane = data.extra_lane(_lane_row({"27": {"theirs": "Mountain View Classic Room",
                                             "hotel_id": 1, "copies_main": True}}))
    assert STANDARD in lane.copies_main and STANDARD not in lane.pairs
    assert STANDARD not in lane.hotels


def test_the_main_lane_never_copies_itself():
    settings = SimpleNamespace(channel="Booking.com", benchmark_room_pairs={},
                               benchmark_undercut=D("100"), benchmark_with_tax=True,
                               benchmark_meal_plan=None)
    assert data.main_lane(settings).copies_main == frozenset()


def test_saved_it_keeps_only_the_flag_and_our_name():
    pair = RoomPair(ours="Deluxe Room", theirs="Classic Room", copies_main=True)
    assert pair.copies_main
    assert RoomPair(theirs="Classic Room").copies_main is False


def test_an_unread_room_still_gets_a_row():
    out = rule.with_unread_rooms([], {28: "Superior King Room"}, "Goibibo")
    assert [p.room_name for p in out] == ["Superior King Room"]
    assert out[0].target is None and "not on sale tonight on Goibibo" in out[0].held


def test_an_unread_room_that_copies_the_main_price_says_so():
    out = rule.with_unread_rooms([], {28: "Superior King Room"}, "Agoda",
                                 copies_main={28}, main_channel="Booking.com")
    assert "Booking.com price" in out[0].held


def test_a_room_already_proposed_is_not_doubled():
    first = rule.with_unread_rooms([], {28: "Superior King Room"}, "Goibibo")
    assert len(rule.with_unread_rooms(first, {28: "Superior King Room"}, "Goibibo")) == 1
