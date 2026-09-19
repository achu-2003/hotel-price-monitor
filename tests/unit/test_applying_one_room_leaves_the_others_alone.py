"""The per-room Apply button writes one rate and does not swallow the next click.

The table's two buttons write every room marked "will apply". "Apply this
room" writes one. Two things have to hold for that to be usable:

    the run is narrowed     only that room's proposal, mapping and typed
                            price survive into the part of the task that
                            opens a browser

    the next click is not   a run for a DIFFERENT room, seconds later, is a
    a redelivery            new run -- not the broker handing back the
                            message that was just processed

The second is the one that bites. ``run_repricing`` treats a finished run for
the same owner and the same night within ``REPEAT_GUARD_MINUTES`` as a
redelivery and returns without touching RMS, which is right for a duplicated
message and wrong for an owner working down the table. An owner applying the
Deluxe and then the Suite a minute later would have seen "done" for a rate
that was never written.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.schemas.repricing import RunIn


class TestWhatThePageMayAskFor:
    def test_a_run_is_the_whole_property_by_default(self):
        assert RunIn().only_room_type_id is None

    def test_one_room_can_be_named(self):
        assert RunIn(only_room_type_id=7).only_room_type_id == 7

    @pytest.mark.parametrize("bad", [0, -3])
    def test_a_room_id_that_is_not_an_id_is_refused(self, bad):
        with pytest.raises(ValueError):
            RunIn(only_room_type_id=bad)

    def test_a_typed_price_still_rides_with_it(self):
        """The row's own box is sent; the rest of the table is not."""
        run = RunIn(only_room_type_id=7, overrides={7: 5200})
        assert run.overrides == {7: 5200}


class TestTheRedeliveryGuardIsScoped:
    """The guard's own logic, with the task's comparison written out.

    Kept as the shape rather than by importing the task, because reaching
    the guard through ``run_repricing`` means Redis, a browser and an RMS
    login -- none of which this is about.
    """

    @staticmethod
    def is_redelivery(last: dict | None, *, check_in: str, scope: str,
                      guard_minutes: int = 10) -> bool:
        if not (last and last.get("status") == "done"
                and last.get("check_in") == check_in
                and last.get("scope", "all") == scope):
            return False
        finished = datetime.fromisoformat(last["updated_at"])
        return (datetime.now(UTC) - finished) < timedelta(minutes=guard_minutes)

    def done(self, scope: str, *, seconds_ago: int = 30, night: str = "2026-09-18") -> dict:
        return {
            "status": "done",
            "scope": scope,
            "check_in": night,
            "updated_at": (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(),
        }

    def test_the_same_room_twice_in_a_minute_is_a_redelivery(self):
        """Still guarded: this is the case the guard exists for."""
        assert self.is_redelivery(
            self.done("room:7"), check_in="2026-09-18", scope="room:7"
        )

    def test_a_different_room_seconds_later_is_a_new_run(self):
        """The bug this scoping fixes: the Suite after the Deluxe."""
        assert not self.is_redelivery(
            self.done("room:7"), check_in="2026-09-18", scope="room:9"
        )

    def test_the_whole_property_after_one_room_is_a_new_run(self):
        assert not self.is_redelivery(
            self.done("room:7"), check_in="2026-09-18", scope="all"
        )

    def test_one_room_after_the_whole_property_is_a_new_run(self):
        assert not self.is_redelivery(
            self.done("all"), check_in="2026-09-18", scope="room:7"
        )

    def test_a_full_run_repeated_is_still_a_redelivery(self):
        assert self.is_redelivery(self.done("all"), check_in="2026-09-18", scope="all")

    def test_a_state_written_before_scoping_existed_reads_as_the_whole_property(self):
        """A run in flight across the deploy has no ``scope`` key.

        It must not read as a room-scoped run, or the first full Apply after
        the deploy would be let through as "different" and the guard would
        have a hole exactly when a redelivery is most likely.
        """
        old = self.done("all")
        del old["scope"]
        assert self.is_redelivery(old, check_in="2026-09-18", scope="all")
        assert not self.is_redelivery(old, check_in="2026-09-18", scope="room:7")

    def test_last_night_is_never_a_redelivery(self):
        assert not self.is_redelivery(
            self.done("room:7", night="2026-09-17"), check_in="2026-09-18", scope="room:7"
        )

    def test_an_old_run_of_the_same_room_is_not_a_redelivery(self):
        """Eleven minutes later the owner meant it."""
        assert not self.is_redelivery(
            self.done("room:7", seconds_ago=11 * 60), check_in="2026-09-18", scope="room:7"
        )


class TestNarrowingTheRun:
    """What the task keeps once it has been told one room.

    The filter itself, applied to the same shapes the task holds: proposals,
    the mapping dict, and the owner's typed prices.
    """

    @staticmethod
    def narrow(proposals, mappings, typed, only):
        if only is None:
            return proposals, mappings, typed
        return (
            [p for p in proposals if p.room_type_id == only],
            {k: v for k, v in mappings.items() if k == only},
            {k: v for k, v in typed.items() if k == only},
        )

    class Proposal:
        def __init__(self, room_type_id):
            self.room_type_id = room_type_id

    def test_only_the_named_room_survives(self):
        props = [self.Proposal(5), self.Proposal(7), self.Proposal(9)]
        got, mappings, typed = self.narrow(
            props, {5: "a", 7: "b", 9: "c"}, {5: 100, 7: 200}, only=7
        )
        assert [p.room_type_id for p in got] == [7]
        assert set(mappings) == {7}

    def test_another_rows_typed_price_cannot_ride_along(self):
        """A half-finished edit three rows down must not be written.

        The page already sends only this row's box; this is the second lock,
        on the worker, for a request that did not come from the page.
        """
        _, _, typed = self.narrow(
            [self.Proposal(7)], {7: "b"}, {5: 9999, 7: 5200}, only=7
        )
        assert typed == {7: 5200}

    def test_no_room_named_leaves_everything(self):
        props = [self.Proposal(5), self.Proposal(7)]
        got, mappings, typed = self.narrow(props, {5: "a", 7: "b"}, {5: 100}, only=None)
        assert len(got) == 2 and len(mappings) == 2 and typed == {5: 100}
