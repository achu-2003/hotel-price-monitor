"""Agoda follows Sterling on Agoda; Booking.com follows them on Booking.com.

The owner's rule -- a hundred under Sterling -- now runs on more than one RMS
channel, and the two are not one number: on 21 Oct 2026 Sterling's Classic
Room was 7,399 on Booking.com and 3,685 on Agoda. So each channel is a LANE
with its own site, gap and room pairs, and three things must hold:

    our side      comes from the lane's site and no other. The rule takes our
                  cheapest price, and our cheapest across two sites is the
                  Agoda one on a night Agoda is cheaper -- after which the
                  Booking.com row in RMS would be scaled by Agoda's discount.

    our room      may have another name there. ASG's "Deluxe Double Room" is
                  "Deluxe Room" on Agoda, a room type of its own with no RMS
                  row; the lane reads it as the mapped room.

    the run       an Agoda Apply writes the Agoda row and nothing else, and
                  the automatic run does each channel whose switch is on.

Pure where it can be; the run is driven down to the grid calls with fakes at
the edges, as in test_other_websites_change_only_when_ticked.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.engines import detect
from app.db.models import RmsRoomMapping
from app.services import repricing as rule
from app.services import repricing_data as data
from app.services.rate_app_rms import RateTypeMissing
from app.workers import tasks_repricing as task

D = Decimal
NIGHT = date(2026, 10, 21)
OURS = SimpleNamespace(id=9, name="ASG HOLIDAY RESORTS")
STERLING = SimpleNamespace(id=10, name="Sterling")
BOOKING_COM, AGODA = 2, 6
DELUXE_DOUBLE, AGODA_DELUXE = 25, 40       # Agoda's "Deluxe Room" got a room type of its own
OPEN = rule.Rule(max_step_pct=D("100"), floor_pct=D("90"), ceiling_pct=D("500"), round_to=10)


def _series(room_type_id, excl, source_id, *, offer="x"):
    return SimpleNamespace(
        room_type_id=room_type_id, is_available=True, source_id=source_id, meal_plan=None,
        last_price_exclusive=D(excl), last_taxes_fees=D("0"), last_price_inclusive=None,
        current_price=None, currency="INR", offer_key=f"{offer}-{source_id}-{room_type_id}",
        check_in=NIGHT, check_out=NIGHT + timedelta(days=1),
    )


def _night():
    """ASG and Sterling on both sites. Agoda is the cheaper site for both."""
    return [
        (_series(DELUXE_DOUBLE, "6951", BOOKING_COM), OURS, "Deluxe Double Room"),
        (_series(AGODA_DELUXE, "4278", AGODA), OURS, "Deluxe Room"),
        (_series(30, "7399", BOOKING_COM), STERLING, "Classic Room"),
        (_series(30, "3510", AGODA), STERLING, "Classic Room"),
    ]


def _bench(**kw):
    return rule.Benchmark(hotel_id=10, hotel="Sterling", undercut=D("100"), with_tax=False,
                          room_pairs={DELUXE_DOUBLE: "Classic Room"}, **kw)


def _lane(**kw):
    base = dict(channel="Agoda", source_id=AGODA, main=False, auto=False, with_tax=False,
                pairs={DELUXE_DOUBLE: "Classic Room"}, ours={DELUXE_DOUBLE: "Deluxe Room"})
    base.update(kw)
    return data.Lane(**base)


def _propose(rows):
    return {p.room_type_id: p for p in rule.propose(
        rows, own_hotel_id=OURS.id, rule=OPEN, room_type_ids={DELUXE_DOUBLE}, benchmark=_bench())}


class TestOurSideComesFromTheLanesSite:
    def test_unpinned_the_cheaper_site_wins_which_is_the_bug(self):
        """Before: our Booking.com room priced off whichever site was cheaper."""
        rows = _night()
        rows[1] = (_series(DELUXE_DOUBLE, "4278", AGODA), OURS, "Deluxe Double Room")
        p = _propose(rows)[DELUXE_DOUBLE]
        assert p.our_price == D("4278"), "our Agoda price, not our Booking.com one"

    def test_the_main_lane_reads_booking_com_only(self):
        rows = _night()
        rows[1] = (_series(DELUXE_DOUBLE, "4278", AGODA), OURS, "Deluxe Double Room")
        main = data.Lane(channel="Booking.com", source_id=BOOKING_COM, main=True, auto=False)
        p = _propose(data.lane_rows(rows, OURS.id, main))[DELUXE_DOUBLE]
        assert p.our_price == D("6951")
        assert p.market == D("7399") and p.target == D("7299")

    def test_a_main_lane_nobody_pinned_behaves_as_before(self):
        main = data.Lane(channel="Booking.com", source_id=None, main=True, auto=False)
        assert len(data.lane_rows(_night(), OURS.id, main)) == 4

    def test_the_agoda_lane_follows_sterling_on_agoda(self):
        p = _propose(data.lane_rows(_night(), OURS.id, _lane()))[DELUXE_DOUBLE]
        assert p.our_price == D("4278")
        assert p.market == D("3510"), "Sterling's Agoda price, not their Booking.com one"
        assert p.target == D("3410")
        assert p.benchmark_entry.source_id == AGODA


class TestOurRoomUnderTheSitesName:
    def test_the_site_name_is_read_as_the_mapped_room(self):
        rows = data.lane_rows(_night(), OURS.id, _lane())
        ours = [s for s, h, _ in rows if h is OURS]
        assert [s.room_type_id for s in ours] == [DELUXE_DOUBLE]

    def test_the_row_itself_is_not_touched(self):
        """A live ORM row would carry the new id into the next commit."""
        night = _night()
        data.lane_rows(night, OURS.id, _lane())
        assert night[1][0].room_type_id == AGODA_DELUXE

    def test_without_a_site_name_the_room_is_matched_on_its_own(self):
        rows = data.lane_rows(_night(), OURS.id, _lane(ours={}))
        assert DELUXE_DOUBLE not in _propose(rows), "Agoda shows no 'Deluxe Double Room'"

    def test_a_named_room_takes_only_that_name(self):
        rows = _night() + [(_series(DELUXE_DOUBLE, "9999", AGODA), OURS, "Deluxe Double Room")]
        p = _propose(data.lane_rows(rows, OURS.id, _lane()))[DELUXE_DOUBLE]
        assert p.our_price == D("4278")


class TestTheLaneFromItsRow:
    def test_pairs_and_names_come_apart(self):
        row = SimpleNamespace(channel="Agoda", source_id=6, auto_enabled=True,
                              benchmark_undercut=D("150"), benchmark_with_tax=True,
                              benchmark_meal_plan=None,
                              room_pairs={"25": {"ours": "Deluxe Room", "theirs": "Classic Room"},
                                          "27": {"ours": None, "theirs": "Mountain View Classic Room"},
                                          "26": {"ours": "Junior Suite", "theirs": None}})
        lane = data.extra_lane(row)
        assert lane.pairs == {25: "Classic Room", 27: "Mountain View Classic Room"}
        assert lane.ours == {25: "Deluxe Room", 26: "Junior Suite"}
        assert lane.undercut == D("150") and lane.auto and not lane.main

    def test_the_benchmark_takes_the_lanes_gap_and_pairs(self):
        b = data.lane_benchmark(SimpleNamespace(id=10, name="Sterling"),
                                _lane(undercut=D("250"), meal_plan="Breakfast"))
        assert b.undercut == D("250") and b.meal_plan == "Breakfast"
        assert b.fallback_board == "Room Only"
        assert b.room_pairs == {DELUXE_DOUBLE: "Classic Room"}


class TestTheLogIsReadPerChannel:
    def test_null_belongs_to_the_main_channel_only(self):
        assert "IS NULL" in str(data.on_channel("Booking.com", main=True))
        assert "IS NULL" not in str(data.on_channel("Agoda", main=False))


def test_agodas_share_link_carries_the_property_id():
    url = ("https://www.agoda.com/accom/property?propertyId=18518106&countryId=35"
           "&adults=2&checkIn=2026-10-21&los=1")
    assert detect(url).external_id == "18518106"
    assert detect("https://www.agoda.com/en-in/some-hotel/hotel/yelagiri-in.html").external_id == "some-hotel"


# -- the run ----------------------------------------------------------------
class FakeGrid:
    def __init__(self, cells):
        self.cells = dict(cells)
        self.writes = []
        self.opened = []

    def read(self, page, *, channel, room, rate_type, day_index=0):
        if (channel, room, rate_type) not in self.cells:
            raise RateTypeMissing(f"under {channel} / {room} there is no {rate_type}")
        return self.cells.get((channel, room, rate_type))

    def write(self, page, *, channel, room, rate_type, day_index, amount):
        self.writes.append((channel, room, rate_type, amount))
        self.cells[(channel, room, rate_type)] = amount
        return amount


class FakeSession:
    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add(self, row):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def scalar(self, stmt):
        return self.app if "rate_application" in str(stmt) else None


def _mapping():
    return RmsRoomMapping(owner_user_id=2, room_type_id=25, rms_room="CLASSIC",
                          rate_type_ep="Online Rate EP", rate_type_cp="Online Rate CP",
                          rate_type_map=None, floor_amount=None, ceiling_amount=None, is_enabled=True)


def _proposal(our, target):
    return rule.Proposal(room_type_id=25, room_name="Deluxe Double Room", tier="deluxe",
                         tier_label="Deluxe", our_price=D(our), our_note=None, competitors=(),
                         market=D("0"), target=D(target), exact=True)


@pytest.fixture
def run(monkeypatch):
    def go(mode, *, channel=None, main_auto=True, agoda_auto=False, agoda_ep=True):
        cells = {
            ("Booking.com", "CLASSIC", "Online Rate EP"): D("9430"),
            ("Booking.com", "CLASSIC", "Online Rate CP"): D("9930"),
            ("Agoda", "CLASSIC", "Online Rate EP"): D("8000"),
            ("Agoda", "CLASSIC", "Online Rate CP"): D("8500"),
        }
        if not agoda_ep:
            del cells[("Agoda", "CLASSIC", "Online Rate EP")]
        grid = FakeGrid(cells)
        settings = SimpleNamespace(channel="Booking.com", round_to=10, known_channels=[],
                                   benchmark_meal_plan=None, auto_enabled=main_auto)
        app = SimpleNamespace(login_url="x", client_number="1", username="u", encrypted_password="p",
                              encrypted_session_state=None)
        agoda = _lane(auto=agoda_auto)
        states: dict = {}

        def compute(s, o, ci, co, lane=None):
            # Booking.com: ours 6,951 -> 6,000. Agoda: ours 4,278 -> 3,410.
            p = _proposal("4278", "3410") if lane is not None else _proposal("6951", "6000")
            return [p], settings, {25: _mapping()}

        monkeypatch.setattr(task, "compute", compute)
        monkeypatch.setattr(task, "extra_lanes", lambda s, o: [agoda])
        monkeypatch.setattr(task, "settings_for", lambda s, o: settings)
        monkeypatch.setattr(task, "shadow_advice", lambda *a, **k: 0)
        monkeypatch.setattr(task, "sync_session", lambda: FakeSession(app))
        monkeypatch.setattr(task, "decrypt", lambda v: "secret")
        monkeypatch.setattr(task.state, "read", lambda o, k: None)
        monkeypatch.setattr(task.state, "write",
                            lambda o, status, kind, **f: states.update(status=status, **f) or dict(states))
        monkeypatch.setattr(task, "open_grid", lambda page, channel: grid.opened.append(channel))
        monkeypatch.setattr(task, "expand_channel", lambda page, channel: grid.opened.append(channel))
        monkeypatch.setattr(task, "channel_names", lambda page: ["Booking.com", "Agoda"])
        monkeypatch.setattr(task, "day_heading", lambda page, i: "Wed 21 Oct")
        monkeypatch.setattr(task, "read_rate", grid.read)
        monkeypatch.setattr(task, "write_rate", grid.write)
        monkeypatch.setattr(task, "_screenshot", lambda page, o: None)
        monkeypatch.setattr(task, "attempt_login",
                            lambda **kw: kw["after_login"](object(), SimpleNamespace(
                                ok=True, message="ok", storage_state=None)))
        grid.result = task.run_repricing.run(2, mode, {}, None, {}, [], [], channel=channel)
        return grid

    return go


def channels_written(grid):
    return {ch for ch, *_ in grid.writes}


def test_an_agoda_apply_writes_the_agoda_row_and_nothing_else(run):
    grid = run("manual", channel="Agoda")
    assert channels_written(grid) == {"Agoda"}
    # 3,410 from 4,278 at an EP of 8,000 is 6,376; CP keeps its 500 over it.
    assert ("Agoda", "CLASSIC", "Online Rate EP", D("6376")) in grid.writes
    assert ("Agoda", "CLASSIC", "Online Rate CP", D("6880")) in grid.writes
    assert "Agoda CLASSIC EP" in grid.result["message"]


def test_the_booking_com_apply_is_unchanged(run):
    grid = run("manual")
    assert channels_written(grid) == {"Booking.com"}


def test_automatic_does_only_the_channels_switched_on(run):
    assert channels_written(run("auto", main_auto=True, agoda_auto=False)) == {"Booking.com"}
    assert channels_written(run("auto", main_auto=False, agoda_auto=True)) == {"Agoda"}
    both = run("auto", main_auto=True, agoda_auto=True)
    assert channels_written(both) == {"Booking.com", "Agoda"}
    assert both.opened == ["Booking.com", "Agoda"]


def test_automatic_with_every_switch_off_opens_nothing(run):
    grid = run("auto", main_auto=False, agoda_auto=False)
    assert grid.writes == [] and grid.opened == []
    assert "Nothing to set" in grid.result["message"]


def test_an_agoda_preview_reads_and_writes_nothing(run):
    grid = run("dry_run", channel="Agoda")
    assert grid.writes == [] and grid.opened == ["Agoda"]


def test_a_channel_with_no_rule_is_refused(run):
    grid = run("manual", channel="Goibibo")
    assert grid.writes == [] and grid.result["ok"] is False
    assert "No rule is saved for Goibibo" in grid.result["message"]


def test_a_plan_the_channel_does_not_sell_is_skipped_not_fatal(run):
    """Under Agoda, RMS lists CLASSIC with CP and MAP rows and no EP. The
    first Preview against the real grid failed the whole room on that; the
    CP row is the one Agoda sells, so it is the one scaled."""
    grid = run("manual", channel="Agoda", agoda_ep=False)
    assert grid.result["failed"] == 0
    # 3,410 from 4,278 at a CP of 8,500 is 6,775.
    assert grid.writes == [("Agoda", "CLASSIC", "Online Rate CP", D("6775"))]
