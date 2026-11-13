"""A site that throttles us is held as a whole, and longer each time.

13 Nov 2026: Goibibo refused every page from our server from 08:30, and each
of its three hotels went on trying every fifteen minutes on its own.
"""
from __future__ import annotations

import pytest

from app.core import ratelimit


class FakeRedis:
    def __init__(self):
        self.ttls: dict[str, int] = {}
        self.values: dict[str, int] = {}

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def setex(self, key, seconds, value):
        self.ttls[key] = seconds
        self.values[key] = value

    def delete(self, *keys):
        for k in keys:
            self.ttls.pop(k, None)
            self.values.pop(k, None)

    def run_out(self, key):
        """The hold's time passes."""
        self.ttls.pop(key, None)
        self.values.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ratelimit, "get_redis", lambda: fake)
    return fake


def test_each_refusal_after_a_hold_doubles_it_up_to_two_hours(redis):
    holds = []
    for _ in range(6):
        holds.append(ratelimit.hold_source(7, 900))
        redis.run_out(ratelimit._hold_key(7))
    assert holds == [900, 1800, 3600, 7200, 7200, 7200]


def test_refusals_while_a_hold_is_running_do_not_escalate_it(redis):
    """Three hotels refused in the same minute are one refusal."""
    assert ratelimit.hold_source(7, 900) == 900
    assert ratelimit.hold_source(7, 900) == 900
    assert ratelimit.hold_source(7, 900) == 900
    assert redis.values[ratelimit._hold_level_key(7)] == 1
    assert ratelimit.source_hold_remaining(7) == 900


def test_a_success_starts_again_from_fifteen_minutes(redis):
    ratelimit.hold_source(7, 900)
    redis.run_out(ratelimit._hold_key(7))
    ratelimit.hold_source(7, 900)
    ratelimit.clear_source_hold(7)
    assert ratelimit.source_hold_remaining(7) == 0
    assert ratelimit.hold_source(7, 900) == 900


def test_other_sources_are_not_held(redis):
    ratelimit.hold_source(7, 900)
    assert ratelimit.source_hold_remaining(8) == 0
