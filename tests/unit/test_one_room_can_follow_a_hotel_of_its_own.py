"""A room of ours can follow another hotel than the benchmark.

Sterling sells only Classic rooms. ASG's Family Suite (two bedrooms, two
bathrooms, a kitchen) has nothing there to compete with, and on 3 Nov 2026
the owner chose Ananthyam's Family Room for it, on Booking.com, Agoda and
Goibibo, while the doubles stayed on Sterling. So a pair may name its own
hotel, and four things must hold:

    the price     the room is priced off THAT hotel's room, at the lane's gap;
                  the other rooms still follow the benchmark.

    the re-check  a pair's hotel is checked like the benchmark: one that is no
                  longer this owner's active competitor unpairs the room rather
                  than letting the same room name at Sterling stand in.

    the storage   pairs stored as a bare name -- every pair until now -- read
                  exactly as before.

    the trigger   a fresh reading of that hotel reprices at once, as a reading
                  of the benchmark does.

    the board     a pair may be compared on another board than the lane's.
                  Booking.com's rule compares breakfast, but Ananthyam sells
                  its Family Room there room-only; the owner chose room-only
                  against room-only for that one room.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services import repricing as rule
from app.services import repricing_data as data
from app.workers import tasks_repricing as task

D = Decimal
NIGHT = date(2026, 11, 3)
OURS = SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS")
STERLING = SimpleNamespace(id=10, name="Sterling")
ANANTHYAM = SimpleNamespace(id=1, name="Ananthyam Resort")
BOOKING_COM = 2
DELUXE_DOUBLE, FAMILY_SUITE = 25, 29
OPEN = rule.Rule(max_step_pct=D("100"), floor_pct=D("10"), ceiling_pct=D("500"), round_to=10)


def _series(room_type_id, excl, *, hotel):
    return SimpleNamespace(
        room_type_id=room_type_id, is_available=True, source_id=BOOKING_COM, meal_plan=None,
        last_price_exclusive=D(excl), last_taxes_fees=D("0"), last_price_inclusive=None,
        current_price=None, currency="INR", offer_key=f"{hotel}-{room_type_id}",
        check_in=NIGHT, check_out=NIGHT + timedelta(days=1),
    )


def _night():
    return [
        (_series(DELUXE_DOUBLE, "5000", hotel=9), OURS, "Deluxe Double Room"),
        (_series(FAMILY_SUITE, "10500", hotel=9), OURS, "Family Suite"),
        (_series(30, "4800", hotel=10), STERLING, "Classic Room"),
        (_series(31, "15000", hotel=1), ANANTHYAM, "Family Room"),
        # The same name at the benchmark must never stand in for Ananthyam's.
        (_series(32, "3000", hotel=10), STERLING, "Family Room"),
    ]


def _bench(**kw):
    base = dict(hotel_id=10, hotel="Sterling", undercut=D("100"), with_tax=False,
                room_pairs={DELUXE_DOUBLE: "Classic Room", FAMILY_SUITE: "Family Room"})
    base.update(kw)
    return rule.Benchmark(**base)


def _propose(benchmark):
    return {p.room_type_id: p for p in rule.propose(
        _night(), own_hotel_id=OURS.id, rule=OPEN,
        room_type_ids={DELUXE_DOUBLE, FAMILY_SUITE}, benchmark=benchmark)}


class TestTheRoomFollowsItsOwnHotel:
    def test_the_family_suite_follows_ananthyam(self):
        p = _propose(_bench(room_hotels={FAMILY_SUITE: (1, "Ananthyam Resort")}))[FAMILY_SUITE]
        assert p.market == D("15000")
        assert p.wanted == D("14900")
        assert p.benchmark == "Ananthyam Resort"

    def test_the_other_rooms_still_follow_the_benchmark(self):
        p = _propose(_bench(room_hotels={FAMILY_SUITE: (1, "Ananthyam Resort")}))[DELUXE_DOUBLE]
        assert p.market == D("4800")
        assert p.benchmark == "Sterling"

    def test_without_its_own_hotel_the_name_is_looked_up_at_the_benchmark(self):
        assert _propose(_bench())[FAMILY_SUITE].market == D("3000")

    def test_a_room_not_on_sale_there_is_held_in_that_hotels_name(self):
        rows = [r for r in _night() if r[1] is not ANANTHYAM]
        p = {p.room_type_id: p for p in rule.propose(
            rows, own_hotel_id=OURS.id, rule=OPEN, room_type_ids={FAMILY_SUITE},
            benchmark=_bench(room_hotels={FAMILY_SUITE: (1, "Ananthyam Resort")}))}[FAMILY_SUITE]
        assert p.target is None
        assert "Ananthyam Resort's \"Family Room\" is not on sale tonight" in p.held

    def test_every_followed_hotel_is_read(self):
        b = _bench(room_hotels={FAMILY_SUITE: (1, "Ananthyam Resort")})
        assert b.hotel_ids == (10, 1)


class TestThePairsHotelIsRechecked:
    def test_a_valid_hotel_is_named_on_the_room(self):
        b = data.with_pair_hotels(_bench(), {FAMILY_SUITE: 1},
                                  [SimpleNamespace(id=1, name="Ananthyam Resort")])
        assert b.followed(FAMILY_SUITE) == (1, "Ananthyam Resort")
        assert b.followed(DELUXE_DOUBLE) == (10, "Sterling")

    def test_a_hotel_that_failed_the_check_unpairs_the_room(self):
        b = data.with_pair_hotels(_bench(), {FAMILY_SUITE: 1}, [])
        assert FAMILY_SUITE not in b.room_pairs
        p = _propose(b)[FAMILY_SUITE]
        assert p.target is None and "not paired" in p.held

    def test_naming_the_benchmark_itself_changes_nothing(self):
        b = data.with_pair_hotels(_bench(), {FAMILY_SUITE: 10}, [])
        assert b.room_pairs[FAMILY_SUITE] == "Family Room" and not b.room_hotels


class TestStoredPairs:
    def _settings(self, pairs):
        return SimpleNamespace(channel="Booking.com", source_id=BOOKING_COM, auto_enabled=False,
                               benchmark_undercut=D("100"), benchmark_with_tax=True,
                               benchmark_meal_plan="Breakfast", benchmark_room_pairs=pairs)

    def test_a_bare_name_reads_as_before(self):
        lane = data.main_lane(self._settings({"25": "Classic Room"}))
        assert lane.pairs == {25: "Classic Room"} and lane.hotels == {}

    def test_a_pair_with_a_hotel(self):
        lane = data.main_lane(self._settings(
            {"25": "Classic Room", "29": {"theirs": "Family Room", "hotel_id": 1}}))
        assert lane.pairs == {25: "Classic Room", 29: "Family Room"}
        assert lane.hotels == {29: 1}

    def test_a_channel_rule_pair_with_a_hotel(self):
        lane = data.extra_lane(SimpleNamespace(
            channel="Goibibo", source_id=7, auto_enabled=True, benchmark_undercut=D("100"),
            benchmark_with_tax=True, benchmark_meal_plan="Breakfast",
            room_pairs={"29": {"ours": "Suite Room", "theirs": "Family Room", "hotel_id": 1},
                        "25": {"ours": "Classic Room", "theirs": "Classic Room"}}))
        assert lane.pairs == {29: "Family Room", 25: "Classic Room"}
        assert lane.hotels == {29: 1}
        assert lane.ours == {29: "Suite Room", 25: "Classic Room"}

    def test_main_benchmark_takes_the_names_of_both_kinds(self):
        b = data.benchmark_for(SimpleNamespace(id=10, name="Sterling"), self._settings(
            {"25": "Classic Room", "29": {"theirs": "Family Room", "hotel_id": 1}}))
        assert b.room_pairs == {25: "Classic Room", 29: "Family Room"}


class TestAReadingOfThatHotelRepricesAtOnce:
    class _Session:
        def __init__(self, direct, automatic):
            self.answers = [direct, automatic]

        def scalars(self, _stmt):
            return iter(self.answers.pop(0))

    def test_an_owner_whose_room_follows_it_is_found(self, monkeypatch):
        monkeypatch.setattr(task, "settings_for", lambda s, o: SimpleNamespace(
            benchmark_room_pairs={"29": {"theirs": "Family Room", "hotel_id": 1}}))
        monkeypatch.setattr(task, "extra_lanes", lambda s, o: [])
        assert task.owners_following(self._Session([], [2]), 1) == [2]

    def test_on_another_channel_too(self, monkeypatch):
        monkeypatch.setattr(task, "settings_for",
                            lambda s, o: SimpleNamespace(benchmark_room_pairs={}))
        monkeypatch.setattr(task, "extra_lanes",
                            lambda s, o: [data.Lane(channel="Agoda", source_id=6, main=False,
                                                    auto=True, hotels={29: 1})])
        assert task.owners_following(self._Session([], [2]), 1) == [2]

    def test_an_owner_following_nobody_there_is_not(self, monkeypatch):
        monkeypatch.setattr(task, "settings_for",
                            lambda s, o: SimpleNamespace(benchmark_room_pairs={"25": "Classic Room"}))
        monkeypatch.setattr(task, "extra_lanes", lambda s, o: [])
        assert task.owners_following(self._Session([], [2]), 1) == []


def _boarded(room_type_id, excl, *, hotel, plan):
    series = _series(room_type_id, excl, hotel=hotel)
    series.meal_plan = plan
    series.offer_key = f"{series.offer_key}-{plan}"
    return series


def _boarded_night():
    return [
        (_boarded(DELUXE_DOUBLE, "5600", hotel=9, plan="Breakfast"), OURS, "Deluxe Double Room"),
        (_boarded(FAMILY_SUITE, "14000", hotel=9, plan="Breakfast"), OURS, "Family Suite"),
        (_boarded(FAMILY_SUITE, "12390", hotel=9, plan="Room Only"), OURS, "Family Suite"),
        (_boarded(30, "5700", hotel=10, plan="Breakfast"), STERLING, "Classic Room"),
        (_boarded(31, "17700", hotel=1, plan="Room Only"), ANANTHYAM, "Family Room"),
    ]


class TestAPairWithABoardOfItsOwn:
    def _propose(self, **kw):
        b = _bench(meal_plan="Breakfast", fallback_board="Room Only",
                   room_hotels={FAMILY_SUITE: (1, "Ananthyam Resort")}, **kw)
        return {p.room_type_id: p for p in rule.propose(
            _boarded_night(), own_hotel_id=OURS.id, rule=OPEN,
            room_type_ids={DELUXE_DOUBLE, FAMILY_SUITE}, benchmark=b)}

    def test_on_the_lanes_board_there_is_nothing_to_follow(self):
        p = self._propose()[FAMILY_SUITE]
        assert p.target is None and "not on sale tonight with breakfast" in p.held

    def test_both_sides_are_read_on_its_own_board(self):
        p = self._propose(room_boards={FAMILY_SUITE: "Room Only"})[FAMILY_SUITE]
        assert p.our_price == D("12390") and p.market == D("17700")
        assert p.wanted == D("17600")
        assert p.board == "Room Only" and p.their_board == "Room Only"

    def test_the_other_rooms_keep_the_lanes_board(self):
        p = self._propose(room_boards={FAMILY_SUITE: "Room Only"})[DELUXE_DOUBLE]
        assert p.board == "Breakfast" and p.market == D("5700") and p.their_board is None

    def test_the_board_is_read_from_the_stored_pair(self):
        lane = data.main_lane(TestStoredPairs()._settings(
            {"29": {"theirs": "Family Room", "hotel_id": 1, "board": "Room Only"}}))
        assert lane.boards == {29: "Room Only"}
        b = data.benchmark_for(SimpleNamespace(id=10, name="Sterling"), TestStoredPairs()._settings(
            {"29": {"theirs": "Family Room", "hotel_id": 1, "board": "Room Only"}}))
        assert b.room_boards == {29: "Room Only"}
