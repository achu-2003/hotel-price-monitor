"""Other RMS channels are written only when the owner ticks them.

RMS carries each room under several channels -- Booking.com, Goibibo,
Expedia, the property's Book Now button. The repricer used to write only the
settings' channel. Now the owner can tick others for a room on the
Repricing page, and the run must do exactly that and nothing more:

    ticked       written, with the RMS rate the page showed, and the
                 channel's other plans keep their supplement over it
    unticked     read on a Preview, never written
    main off     the owner can untick Booking.com for a room; it is read
                 and left as it is
    limits       the room's RMS floor still holds on every channel
    automatic    the scheduler never touches another channel, whatever the
                 page last sent

The task runs for real here, down to the grid calls; only the edges are
fakes -- the login, Redis, the database session and the RMS grid itself --
so no browser opens and no row reaches the real database.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.db.models import RmsRoomMapping
from app.services import repricing as rule
from app.services.repricing import same_share
from app.workers import tasks_repricing as task

D = Decimal
MAIN = "Booking.com"


class FakeGrid:
    """The RMS grid as a dict: ``(channel, room, rate_type) -> rate``."""

    def __init__(self, cells: dict[tuple[str, str, str], Decimal]):
        self.cells = dict(cells)
        self.writes: list[tuple[str, str, str, Decimal]] = []
        self.opened: list[str] = []

    def channels(self) -> list[str]:
        seen = []
        for ch, _, _ in self.cells:
            if ch not in seen:
                seen.append(ch)
        return seen

    def read(self, page, *, channel, room, rate_type, day_index=0):
        return self.cells.get((channel, room, rate_type))

    def write(self, page, *, channel, room, rate_type, day_index, amount):
        self.writes.append((channel, room, rate_type, amount))
        self.cells[(channel, room, rate_type)] = amount
        return amount


class FakeSession:
    """Collects what the task adds; answers the two lookups it makes."""

    rows: list = []

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add(self, row):
        FakeSession.rows.append(row)

    def commit(self):
        pass

    def rollback(self):
        pass

    def scalar(self, stmt):
        return self.app if "rate_application" in str(stmt) else None

    def scalars(self, stmt):
        return []


def mapping(room_type_id=27, *, floor=None) -> RmsRoomMapping:
    return RmsRoomMapping(owner_user_id=2, room_type_id=room_type_id, rms_room="DELUXE",
                          rate_type_ep=None, rate_type_cp="Online Rate CP", rate_type_map="Online Rate MAP",
                          floor_amount=floor, ceiling_amount=None, is_enabled=True)


def proposal(room_type_id=27, *, our="6358", target="6000") -> rule.Proposal:
    return rule.Proposal(room_type_id=room_type_id, room_name="Standard Double Room", tier="classic",
                         tier_label="Classic Room", our_price=D(our), our_note=None, competitors=(),
                         market=D("3878"), target=D(target))


@pytest.fixture
def run(monkeypatch):
    """``run(grid, mode, channels=..., skip=..., floor=...)`` -> the grid afterwards."""

    def go(grid: FakeGrid, mode: str, *, channels=None, skip=None, floor=None):
        settings = SimpleNamespace(channel=MAIN, round_to=10, known_channels=[],
                                   benchmark_meal_plan=None)
        app = SimpleNamespace(login_url="x", client_number="1", username="u", encrypted_password="p",
                              encrypted_session_state=None)
        FakeSession.rows = []
        states: dict = {}
        m = mapping(floor=floor)
        monkeypatch.setattr(task, "compute", lambda s, o, ci, co: ([proposal()], settings, {27: m}))
        monkeypatch.setattr(task, "settings_for", lambda s, o: settings)
        monkeypatch.setattr(task, "shadow_advice", lambda *a, **k: 0)
        monkeypatch.setattr(task, "sync_session", lambda: FakeSession(app))
        monkeypatch.setattr(task, "decrypt", lambda v: "secret")
        monkeypatch.setattr(task.state, "read", lambda o, k: None)
        monkeypatch.setattr(task.state, "write", lambda o, status, kind, **f: states.update(status=status, **f) or dict(states))
        monkeypatch.setattr(task, "open_grid", lambda page, channel: grid.opened.append(channel))
        monkeypatch.setattr(task, "expand_channel", lambda page, channel: grid.opened.append(channel))
        monkeypatch.setattr(task, "channel_names", lambda page: grid.channels())
        monkeypatch.setattr(task, "day_heading", lambda page, i: "Fri 18 Sep")
        monkeypatch.setattr(task, "read_rate", grid.read)
        monkeypatch.setattr(task, "write_rate", grid.write)
        monkeypatch.setattr(task, "_screenshot", lambda page, o: None)

        def login(**kw):
            probe = SimpleNamespace(ok=True, message="ok", storage_state=None)
            return kw["after_login"](object(), probe)

        monkeypatch.setattr(task, "attempt_login", login)
        result = task.run_repricing.run(2, mode, {}, None, channels or {}, skip or [])
        grid.result = result
        grid.settings = settings
        return grid

    return go


def rms():
    """Booking.com's DELUXE at 8,500 CP / 11,500 MAP; Goibibo and Expedia dearer."""
    return FakeGrid({
        (MAIN, "DELUXE", "Online Rate CP"): D("8500"), (MAIN, "DELUXE", "Online Rate MAP"): D("11500"),
        ("Goibibo", "DELUXE", "Online Rate CP"): D("9000"), ("Goibibo", "DELUXE", "Online Rate MAP"): D("12000"),
        ("Expedia", "DELUXE", "Online Rate CP"): D("8800"), ("Expedia", "DELUXE", "Online Rate MAP"): D("11800"),
    })


def written(grid, channel):
    return {(rt, amount) for ch, room, rt, amount in grid.writes if ch == channel}


def test_nothing_ticked_writes_only_the_main_channel(run):
    grid = run(rms(), "manual")
    assert written(grid, "Goibibo") == set() and written(grid, "Expedia") == set()
    # 6,000 on the site from 6,358 at 8,500 is 8,020 in RMS; MAP keeps its 3,000.
    assert written(grid, MAIN) == {("Online Rate CP", D("8020")), ("Online Rate MAP", D("11020"))}


def test_a_ticked_channel_gets_exactly_the_rate_shown_and_its_supplement(run):
    grid = run(rms(), "manual", channels={"27": {"Goibibo": "8500"}})
    assert written(grid, "Goibibo") == {("Online Rate CP", D("8500")), ("Online Rate MAP", D("11500"))}
    assert written(grid, "Expedia") == set(), "an unticked channel is never written"


def test_the_main_channel_can_be_left_alone(run):
    grid = run(rms(), "manual", channels={"27": {"Goibibo": "8500"}}, skip=[27])
    assert written(grid, MAIN) == set()
    assert grid.cells[(MAIN, "DELUXE", "Online Rate CP")] == D("8500")
    assert written(grid, "Goibibo")


def test_the_floor_holds_on_every_channel(run):
    grid = run(rms(), "manual", channels={"27": {"Goibibo": "7000"}}, floor=D("7500"))
    assert written(grid, "Goibibo") == set()
    assert "below this room's RMS floor" in grid.result["message"]


def test_booking_com_named_as_another_channel_is_not_written_twice(run):
    grid = run(rms(), "manual", channels={"27": {MAIN: "9999"}})
    assert [w for w in grid.writes if w[3] == D("9999")] == []
    assert sum(1 for w in grid.writes if w[0] == MAIN and w[2] == "Online Rate CP") == 1


def test_automatic_mode_never_touches_another_channel(run):
    grid = run(rms(), "auto", channels={"27": {"Goibibo": "8500"}})
    assert written(grid, "Goibibo") == set()


def test_a_preview_reads_every_channel_and_writes_nothing(run):
    grid = run(rms(), "dry_run", channels={"27": {"Goibibo": "8500"}})
    assert grid.writes == []
    read = {(r.channel, r.rms_rate_type, r.current_rms) for r in FakeSession.rows if r.status == "read"}
    assert ("Goibibo", "Online Rate CP", D("9000")) in read
    assert ("Expedia", "Online Rate MAP", D("11800")) in read
    assert grid.settings.known_channels == [MAIN, "Goibibo", "Expedia"]


def test_the_suggestion_moves_a_channel_by_the_main_channels_share():
    """Booking.com 8,500 -> 7,650 is -10%; Goibibo 9,000 becomes 8,100."""
    assert same_share(D("9000"), D("8500"), D("7650"), round_to=10) == D("8100")
