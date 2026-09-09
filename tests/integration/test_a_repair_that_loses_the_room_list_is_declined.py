"""A repair may not overwrite a working config with a smaller one.

The predicate is unit-tested in ``test_rediscovery.py``. What is proved here is
the thing that actually failed in production on 3 Sep 2026: the TASK declines,
and the stored selectors are still there afterwards.

That distinction matters. ``rediscover_source`` writes to live configuration
with nobody reading the result, and a guard that returns the right answer to a
caller that has already written is no guard at all. So the assertion is on the
row, not on the return value.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.db.models import PriceSeries, RoomType
from app.workers import tasks_repair

#: What the broken 3 Sep repair actually proposed: Treebo's hashed classes,
#: the card selector landing on the container that wraps the room list.
COLLAPSED = {
    "room_card": "div.cikLsc.kyiLCd",
    "selectors": {"price": "div.jaYzsn", "room_name": "div.jptIKY"},
}

#: What was stored before it, and must still be stored after.
WORKING = {
    "room_card": "#t-roomTypes",
    "wait_for": "#t-roomTypes",
    "selectors": {"price": "text=/^₹/", "room_name": "text=/Room \\(/"},
    "discovery_note": "Discovered 2026-08-01: 4 rooms",
}


def _candidate(room_count: int, names: list[str]):
    """A discovery result that clears every bar except the new one.

    Strongly verified, corroborated, priced in rupees -- the point being that
    the old gate would have written this without hesitating.
    """
    return SimpleNamespace(
        ok=True,
        unlearnable=None,
        notes=[],
        cross_sold_names=[],
        best=SimpleNamespace(
            is_strongly_verified=True,
            room_count=room_count,
            sample_names=names,
            sample_prices=[1] * room_count,
            corroborated=room_count,
            corroborated_marked=room_count,
            source_url="https://treebo.test/emerald",
            as_adapter_config=lambda _fragment: dict(COLLAPSED),
        ),
    )


@pytest.fixture
def source_with_four_rooms(session, hotel_fixture):
    """A source already reading four rooms, on the config that reads them."""
    hotel = hotel_fixture["hotel"]
    source = hotel_fixture["source"]
    hotel_source = hotel_fixture["hotel_source"]
    hotel_source.adapter_config = dict(WORKING)

    now = datetime.now(UTC)
    for n, name in enumerate(["Maple", "Oak", "Teak", "Cedar"]):
        room = RoomType(
            hotel_id=hotel.id,
            name=f"Deluxe Room ({name})",
            canonical_name=f"deluxe-{name.lower()}",
            capacity=2,
        )
        session.add(room)
        session.flush()
        session.add(
            PriceSeries(
                offer_key=f"offer-{n}",
                hotel_id=hotel.id,
                room_type_id=room.id,
                source_id=source.id,
                check_in=date(2026, 12, 20),
                check_out=date(2026, 12, 21),
                adults=2,
                currency="INR",
                first_seen_at=now,
                last_checked_at=now,
            )
        )
    session.flush()
    return hotel_source


@pytest.fixture
def repair_against(session, monkeypatch):
    """Run the task against the test session, with discovery stubbed."""

    @contextmanager
    def _session():
        yield session

    monkeypatch.setattr(tasks_repair, "sync_session", _session)
    monkeypatch.setattr(tasks_repair, "local_today", lambda _tz: date(2026, 12, 1))
    monkeypatch.setattr(tasks_repair, "json_fragment", lambda _url: None)

    def run(hotel_source_id, result):
        monkeypatch.setattr(
            "app.adapters.discovery.inspect_url", lambda *a, **kw: result
        )
        return tasks_repair.rediscover_source(
            hotel_source_id,
            check_in="2026-12-20",
            check_out="2026-12-21",
            reason="adapter_config",
        )

    return run


class TestFourRoomsCollapsingToOne:
    """The 3 Sep production failure, as a test that would have caught it."""

    def test_the_repair_is_declined(self, source_with_four_rooms, repair_against):
        outcome = repair_against(
            source_with_four_rooms.id,
            _candidate(1, ["Treebo Premium Emerald Dove with Swimming Pool"]),
        )
        assert outcome["status"] == "regressed"

    def test_it_says_what_it_compared(self, source_with_four_rooms, repair_against):
        outcome = repair_against(
            source_with_four_rooms.id, _candidate(1, ["the property title"])
        )
        assert "1 room(s)" in outcome["why"]
        assert "already reads 4" in outcome["why"]

    def test_the_working_selectors_are_still_stored(
        self, session, source_with_four_rooms, repair_against
    ):
        """The assertion the return value cannot make on its own."""
        repair_against(source_with_four_rooms.id, _candidate(1, ["the title"]))
        session.refresh(source_with_four_rooms)
        stored = source_with_four_rooms.adapter_config
        assert stored["room_card"] == "#t-roomTypes"
        assert stored["selectors"]["room_name"] == "text=/Room \\(/"

    def test_the_rooms_are_left_alone(
        self, session, source_with_four_rooms, repair_against
    ):
        """A declined repair retires nothing.

        ``_retire_invented_rooms`` DELETES room types and cascades to their
        price series. Reaching it on a candidate that found one room would
        destroy the room list this guard exists to defend.
        """
        repair_against(source_with_four_rooms.id, _candidate(1, ["the title"]))
        remaining = session.scalars(
            select_rooms(source_with_four_rooms.hotel_id)
        ).all()
        assert len(remaining) == 5  # the fixture's own room, plus the four


class TestARepairThatNamesTheRoomAfterTheHotel:
    """The fault the count guard cannot see, end to end.

    The fixture hotel is "Test Resort" with four rooms, but this is the
    one-room shape that actually happened: what makes it a regression is not
    how many came back, it is what they were called.
    """

    def test_the_repair_is_declined(self, source_with_four_rooms, repair_against):
        outcome = repair_against(
            source_with_four_rooms.id, _candidate(4, ["Test Resort"] * 4)
        )
        assert outcome["status"] == "named_after_property"

    def test_the_working_selectors_are_still_stored(
        self, session, source_with_four_rooms, repair_against
    ):
        repair_against(source_with_four_rooms.id, _candidate(4, ["Test Resort"] * 4))
        session.refresh(source_with_four_rooms)
        assert source_with_four_rooms.adapter_config["room_card"] == "#t-roomTypes"

    def test_a_full_room_list_that_merely_shares_a_word_is_accepted(
        self, session, source_with_four_rooms, repair_against
    ):
        """One genuinely named suite must not condemn the rooms around it."""
        outcome = repair_against(
            source_with_four_rooms.id,
            _candidate(4, ["Test Resort Suite", "Maple", "Oak", "Teak"]),
        )
        assert outcome["status"] != "named_after_property"


class TestARepairThatIsNotARegression:
    """The guard must not become a reason nothing is ever repaired."""

    def test_a_candidate_reading_every_room_is_accepted(
        self, session, source_with_four_rooms, repair_against
    ):
        outcome = repair_against(
            source_with_four_rooms.id,
            _candidate(4, ["Maple", "Oak", "Teak", "Cedar"]),
        )
        assert outcome["status"] != "regressed"

    def test_losing_one_room_of_four_is_accepted(
        self, session, source_with_four_rooms, repair_against
    ):
        """A hotel really can retire a room type."""
        outcome = repair_against(
            source_with_four_rooms.id, _candidate(3, ["Maple", "Oak", "Teak"])
        )
        assert outcome["status"] != "regressed"


def select_rooms(hotel_id):
    from sqlalchemy import select

    return select(RoomType).where(RoomType.hotel_id == hotel_id)
