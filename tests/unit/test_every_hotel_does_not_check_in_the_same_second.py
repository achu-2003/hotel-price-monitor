"""Ten hotels, one ``next_run_at``, to the microsecond.

WHAT WAS ON THE ROW
===================
Every enabled target in this deployment carried the identical schedule::

    target  1..12   next_run_at = 2026-09-04 15:52:20.505328+05:30

``schedule_next_run`` took one ``now`` for the batch and wrote ``now +
interval`` on all of them, and nothing ever pulled them apart again. One
moment of alignment -- a restart, an outage, the hour the deployment was
seeded -- puts every hotel into the same second of every interval permanently.

WHY IT LOOKED HARMLESS
======================
``dispatch_jitter_seconds`` staggers the ENQUEUE by up to three minutes, so
the fetches do not literally start together and nothing looked wrong. But that
window is fixed while the number of hotels is not. Two browser workers and a
Playwright fetch of roughly thirty seconds fit about a dozen checks into it;
past that the surplus tasks sit in the queue until they pass the ``expires``
deadline the dispatcher sets, and the broker drops them. A dropped task writes
no check_run and no monitoring_error, so coverage thins out with nothing on any
screen to say so.

THE ONE THING THE FIX MUST NOT DO
=================================
A group is the set of targets that share a single page load -- same source,
same stay window, different occupancies. The dispatcher calls this once per
group. Spreading targets WITHIN a group would split one fetch into several and
cost exactly what the grouping was built to save, so the offset is drawn once
per call and handed to every target in it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import monitoring

NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


class _Session:
    """Just enough of a Session for ``schedule_next_run``: it selects, and the
    objects it gets back are mutated in place."""

    def __init__(self, targets):
        self._targets = targets

    def execute(self, _statement):
        targets = self._targets

        class _Result:
            def scalars(self):
                return SimpleNamespace(all=lambda: targets)

        return _Result()


def _targets(count: int, interval: int = 30):
    return [
        SimpleNamespace(id=i, interval_minutes=interval, next_run_at=None)
        for i in range(1, count + 1)
    ]


class TestOneGroupStaysTogether:
    """The saving this must not undo."""

    def test_every_target_in_one_call_gets_the_same_time(self):
        targets = _targets(3)
        monitoring.schedule_next_run(_Session(targets), [t.id for t in targets], NOW)
        assert len({t.next_run_at for t in targets}) == 1

    def test_it_holds_when_the_offset_happens_to_be_large(self):
        # Whatever the draw, the group moves as one -- so pin it at the top of
        # the range rather than trusting a lucky random.
        import app.services.monitoring as m

        original, m.random.random = m.random.random, lambda: 1.0
        try:
            targets = _targets(4)
            monitoring.schedule_next_run(_Session(targets), [t.id for t in targets], NOW)
        finally:
            m.random.random = original
        assert len({t.next_run_at for t in targets}) == 1


class TestSeparateGroupsDrift:
    """The lockstep that was the whole problem."""

    def test_two_calls_at_the_same_instant_do_not_agree(self):
        seen = set()
        for _ in range(25):
            targets = _targets(1)
            monitoring.schedule_next_run(_Session(targets), [1], NOW)
            seen.add(targets[0].next_run_at)
        # Twenty-five independent draws collapsing to one value would mean the
        # offset is not being drawn at all.
        assert len(seen) > 1


class TestTheOffsetStaysSmall:
    """A schedule nobody asked to change must not change much."""

    def test_it_is_never_earlier_than_the_interval(self):
        for _ in range(50):
            targets = _targets(1, interval=30)
            monitoring.schedule_next_run(_Session(targets), [1], NOW)
            assert targets[0].next_run_at >= NOW + timedelta(minutes=30)

    def test_it_is_never_more_than_five_percent_late(self):
        for _ in range(50):
            targets = _targets(1, interval=30)
            monitoring.schedule_next_run(_Session(targets), [1], NOW)
            assert targets[0].next_run_at <= NOW + timedelta(minutes=31.5)

    def test_the_bound_is_a_fraction_of_the_interval_not_a_fixed_number(self):
        # A five-minute target must not be pushed by the same 90 seconds a
        # thirty-minute one can absorb.
        import app.services.monitoring as m

        original, m.random.random = m.random.random, lambda: 1.0
        try:
            targets = _targets(1, interval=5)
            monitoring.schedule_next_run(_Session(targets), [1], NOW)
        finally:
            m.random.random = original
        assert targets[0].next_run_at == NOW + timedelta(minutes=5.25)
