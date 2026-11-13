"""A site that refuses us is read one page at a time, at the pace it accepts.

Goibibo, 10-13 Nov 2026: loads under ten minutes after the previous one got
rooms about half the time, loads ten minutes or more after it about 80%.
"""
from __future__ import annotations

import pytest

from app.core import ratelimit


class FakeRedis:
    """Just enough Redis: plain keys, and the pacing script run in Python."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key):
        v = self.values.get(key)
        return v.encode() if v is not None else None

    def set(self, key, value, ex=None):
        self.values[key] = str(value)

    def delete(self, *keys):
        for k in keys:
            self.values.pop(k, None)

    def eval(self, script, numkeys, gap_key, last_key, now, ttl):
        assert script is ratelimit._PACE_TAKE_LUA
        if gap_key not in self.values:
            return "0"
        wait = float(self.values.get(last_key, "0")) + float(self.values[gap_key]) - now
        if wait > 0:
            return str(wait)
        self.values[last_key] = str(now)
        return "0"


@pytest.fixture
def clock(monkeypatch):
    fake = FakeRedis()
    now = [1_000_000.0]
    monkeypatch.setattr(ratelimit, "get_redis", lambda: fake)
    monkeypatch.setattr(ratelimit.time, "time", lambda: now[0])
    return now


def test_a_site_that_never_refused_is_not_paced(clock):
    assert ratelimit.pace_take(7) == 0
    assert ratelimit.pace_take(7) == 0


def test_a_refusal_spaces_the_next_load_at_least_five_minutes(clock):
    assert ratelimit.pace_refused(7) == 300
    assert ratelimit.pace_take(7) == pytest.approx(300)
    clock[0] += 300
    assert ratelimit.pace_take(7) == 0


def test_one_load_at_a_time_while_paced(clock):
    """Three hotels due together: the first goes, the others wait their turn."""
    ratelimit.pace_refused(7)
    clock[0] += 300
    assert ratelimit.pace_take(7) == 0
    assert ratelimit.pace_take(7) == pytest.approx(300)
    assert ratelimit.pace_take(7) == pytest.approx(300)


def test_refusals_double_the_gap_up_to_thirty_minutes(clock):
    gaps = [ratelimit.pace_refused(7) for _ in range(5)]
    assert gaps == [300, 600, 1200, 1800, 1800]


def test_successes_narrow_it_until_pacing_switches_itself_off(clock):
    ratelimit.pace_refused(7)
    ratelimit.pace_succeeded(7, base_seconds=60)
    assert ratelimit.pace_gap(7) == pytest.approx(255)
    for _ in range(20):
        ratelimit.pace_succeeded(7, base_seconds=60)
    assert ratelimit.pace_gap(7) == 0
    assert ratelimit.pace_take(7) == 0


def test_other_sources_are_not_paced(clock):
    ratelimit.pace_refused(7)
    assert ratelimit.pace_take(8) == 0
