"""Tests for the day-over-day (carry-over) comparison.

The intraday state machine answers "did the rate for THIS night move?". This
one answers "did the rate for tonight move against last night?" — the question
a ``lead_time_days = 0`` target actually asks, and the one that produced zero
alerts before this existed, because every new stay date is a first sighting.

The false-alert cases matter most here. This comparison runs without a
debounce, so anything it lets through goes straight to someone's screen.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db.models.enums import ChangeDirection
from app.services.comparison import (
    CarryOver,
    Observation,
    Thresholds,
    compare_across_stay_dates,
)

D = Decimal
DEFAULT = Thresholds()  # ₹50 and 2%, the shipped defaults

YESTERDAY = date(2026, 8, 18)
TODAY = date(2026, 8, 19)


def previous(price, *, available=True, check_in=YESTERDAY, key="prevkey") -> CarryOver:
    return CarryOver(
        last_price=None if price is None else D(price),
        is_available=available,
        check_in=check_in,
        offer_key=key,
    )


# -- the case this feature exists for --------------------------------
def test_overnight_increase_is_reported():
    """A Roys Kozee Kaves shape: the room moved overnight by ~10%.

    Note the pipeline compares CONFIRMED baselines, not raw observations,
    so the live numbers differ slightly from any single reading — an
    insignificant intraday drift deliberately leaves `last_price` alone.
    """
    result = compare_across_stay_dates(
        previous("1023.75"), Observation(price=D("1121.25")), DEFAULT, this_check_in=TODAY
    )

    assert result is not None
    assert result.direction is ChangeDirection.INCREASE
    assert result.old_price == D("1023.75")
    assert result.new_price == D("1121.25")
    assert result.delta == D("97.50")
    assert result.delta_pct == D("9.52")
    assert result.previous_offer_key == "prevkey"
    assert result.previous_check_in == YESTERDAY


def test_overnight_decrease_is_reported():
    result = compare_across_stay_dates(
        previous("3640"), Observation(price=D("3400")), DEFAULT, this_check_in=TODAY
    )

    assert result is not None
    assert result.direction is ChangeDirection.DECREASE
    assert result.delta == D("-240.00")


# -- silence, and the reasons for it ---------------------------------
def test_no_previous_stay_date_is_silent():
    """The first night ever monitored has no baseline. Adding a hotel must not
    immediately alert."""
    assert compare_across_stay_dates(None, Observation(price=D("2000")), DEFAULT) is None


def test_unpriced_previous_night_is_silent():
    assert (
        compare_across_stay_dates(
            previous(None), Observation(price=D("2000")), DEFAULT, this_check_in=TODAY
        )
        is None
    )


def test_identical_price_is_silent():
    assert (
        compare_across_stay_dates(
            previous("2000"), Observation(price=D("2000")), DEFAULT, this_check_in=TODAY
        )
        is None
    )


def test_below_threshold_is_silent():
    """₹16.25 on a ₹2,340 room — the real dip that should NOT alert. It clears
    neither ₹50 nor 2%."""
    assert (
        compare_across_stay_dates(
            previous("2340"), Observation(price=D("2323.75")), DEFAULT, this_check_in=TODAY
        )
        is None
    )


def test_must_clear_both_floors():
    """₹60 on a ₹6,000 room is 1% — over the rupee floor, under the percentage
    one. Both are required, exactly as in the intraday path."""
    assert (
        compare_across_stay_dates(
            previous("6000"), Observation(price=D("6060")), DEFAULT, this_check_in=TODAY
        )
        is None
    )


# -- sold out is never a price ---------------------------------------
def test_previously_sold_out_is_silent():
    """A sold-out night keeps its last price in the series so it can be
    restored. Comparing against it would invent a change from a room that was
    never on sale."""
    assert (
        compare_across_stay_dates(
            previous("2000", available=False),
            Observation(price=D("3000")),
            DEFAULT,
            this_check_in=TODAY,
        )
        is None
    )


def test_sold_out_tonight_is_silent():
    """Sold out is an availability event with its own handling — never a price
    that dropped to zero."""
    assert (
        compare_across_stay_dates(
            previous("2000"),
            Observation(price=None, is_available=False),
            DEFAULT,
            this_check_in=TODAY,
        )
        is None
    )


# -- the gap window --------------------------------------------------
def test_gap_beyond_the_window_is_silent():
    """The monitor was off for three weeks. The rate did move, but calling it
    an overnight change would be a lie about when it happened."""
    assert (
        compare_across_stay_dates(
            previous("2000", check_in=date(2026, 7, 20)),
            Observation(price=D("3000")),
            DEFAULT,
            this_check_in=TODAY,
        )
        is None
    )


def test_gap_inside_the_window_still_reports():
    """A weekend outage must not silently swallow a real move."""
    result = compare_across_stay_dates(
        previous("2000", check_in=date(2026, 8, 16)),
        Observation(price=D("3000")),
        DEFAULT,
        this_check_in=TODAY,
    )
    assert result is not None
    assert result.previous_check_in == date(2026, 8, 16)


@pytest.mark.parametrize("gap_days", [0, -1])
def test_same_or_future_baseline_is_silent(gap_days):
    """A baseline that is not strictly earlier is not a baseline. Guards
    against a query change that starts returning today's own row."""
    assert (
        compare_across_stay_dates(
            previous("2000", check_in=date(2026, 8, 19 - gap_days)),
            Observation(price=D("3000")),
            DEFAULT,
            this_check_in=TODAY,
        )
        is None
    )


def test_gap_window_is_configurable():
    assert (
        compare_across_stay_dates(
            previous("2000", check_in=date(2026, 8, 1)),
            Observation(price=D("3000")),
            DEFAULT,
            this_check_in=TODAY,
            max_gap_days=30,
        )
        is not None
    )


# -- threshold plumbing ----------------------------------------------
def test_thresholds_are_honoured():
    """The same move, alerting or not depending purely on configured
    sensitivity."""
    move = (previous("2340"), Observation(price=D("2323.75")))
    assert compare_across_stay_dates(*move, DEFAULT, this_check_in=TODAY) is None
    sensitive = Thresholds(min_delta_abs=D("10"), min_delta_pct=D("0.25"))
    assert compare_across_stay_dates(*move, sensitive, this_check_in=TODAY) is not None


def test_zero_baseline_does_not_divide_by_zero():
    result = compare_across_stay_dates(
        previous("0"), Observation(price=D("1500")), DEFAULT, this_check_in=TODAY
    )
    assert result is not None
    assert result.delta_pct == D("100.00")
