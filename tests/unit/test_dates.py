"""Tests for stay-window resolution.

The headline test is ``test_rolling_window_yields_different_nights_each_day``:
it documents exactly why a rolling offset must never be part of the price
identity.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.db.models.enums import DateStrategy
from app.services.dates import (
    StayWindow,
    default_windows,
    next_weekend,
    resolve_stay_window,
)

TODAY = date(2026, 8, 17)  # a Monday


# ── the rolling-date trap ────────────────────────────────────────────
def test_rolling_window_yields_different_nights_each_day():
    """THE bug this design exists to prevent.

    "7 days out" is 24 Aug today and 25 Aug tomorrow. Those are different
    nights, so their prices are NOT comparable. Because the resolved absolute
    dates go into the offer_key, they land in separate series and can never be
    compared against each other by accident.
    """
    today_window = resolve_stay_window(
        strategy=DateStrategy.ROLLING, today=TODAY,
        lead_time_days=7, length_of_stay_nights=1,
    )
    tomorrow_window = resolve_stay_window(
        strategy=DateStrategy.ROLLING, today=TODAY + timedelta(days=1),
        lead_time_days=7, length_of_stay_nights=1,
    )
    assert today_window.check_in == date(2026, 8, 24)
    assert tomorrow_window.check_in == date(2026, 8, 25)
    assert today_window != tomorrow_window


def test_rolling_resolves_to_absolute_dates():
    w = resolve_stay_window(
        strategy=DateStrategy.ROLLING, today=TODAY,
        lead_time_days=7, length_of_stay_nights=2,
    )
    assert w == StayWindow(date(2026, 8, 24), date(2026, 8, 26))
    assert w.nights == 2


def test_rolling_zero_lead_time_is_tonight():
    w = resolve_stay_window(
        strategy=DateStrategy.ROLLING, today=TODAY,
        lead_time_days=0, length_of_stay_nights=1,
    )
    assert w.check_in == TODAY


# ── fixed windows ────────────────────────────────────────────────────
def test_fixed_window_is_returned_as_configured():
    w = resolve_stay_window(
        strategy=DateStrategy.FIXED, today=TODAY,
        fixed_check_in=date(2026, 8, 20), fixed_check_out=date(2026, 8, 21),
    )
    assert w == StayWindow(date(2026, 8, 20), date(2026, 8, 21))


def test_past_fixed_window_stops_being_monitored():
    """Not an error: that night has happened and its price cannot change."""
    w = resolve_stay_window(
        strategy=DateStrategy.FIXED, today=TODAY,
        fixed_check_in=date(2026, 8, 10), fixed_check_out=date(2026, 8, 11),
    )
    assert w is None


def test_fixed_window_starting_today_is_still_monitored():
    w = resolve_stay_window(
        strategy=DateStrategy.FIXED, today=TODAY,
        fixed_check_in=TODAY, fixed_check_out=TODAY + timedelta(days=1),
    )
    assert w is not None


# ── validation ───────────────────────────────────────────────────────
def test_checkout_must_be_after_checkin():
    with pytest.raises(ValueError, match="must be after"):
        StayWindow(date(2026, 8, 21), date(2026, 8, 20))


def test_zero_night_stay_is_rejected():
    with pytest.raises(ValueError):
        StayWindow(date(2026, 8, 20), date(2026, 8, 20))


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (dict(strategy=DateStrategy.FIXED), "fixed_check_in"),
        (dict(strategy=DateStrategy.ROLLING), "lead_time_days"),
        (
            dict(strategy=DateStrategy.ROLLING, lead_time_days=-1, length_of_stay_nights=1),
            "negative",
        ),
        (
            dict(strategy=DateStrategy.ROLLING, lead_time_days=7, length_of_stay_nights=0),
            "at least 1",
        ),
    ],
)
def test_misconfiguration_fails_loudly(kwargs, match):
    """Better a hard error at dispatch than a silently wrong price series."""
    with pytest.raises(ValueError, match=match):
        resolve_stay_window(today=TODAY, **kwargs)


# ── weekend helper ───────────────────────────────────────────────────
def test_next_weekend_from_monday():
    assert next_weekend(TODAY).check_in == date(2026, 8, 21)  # that Friday


def test_next_weekend_from_friday_skips_to_the_following_one():
    """Tonight's rate is effectively already set; next Friday is the useful one."""
    friday = date(2026, 8, 21)
    assert next_weekend(friday).check_in == date(2026, 8, 28)


def test_default_windows_cover_short_medium_and_weekend():
    windows = default_windows(TODAY)
    assert len(windows) == 3
    assert [w.check_in for w in windows] == [
        date(2026, 8, 24),
        date(2026, 8, 31),
        date(2026, 8, 21),
    ]
    assert all(w.nights == 1 for w in windows)
