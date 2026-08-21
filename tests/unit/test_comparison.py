"""Tests for the price comparison state machine.

Covers every outcome and, more importantly, the cases that would produce a
FALSE alert. A system that cries wolf gets muted, and a muted system is worth
nothing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models.enums import ChangeDirection
from app.services.comparison import (
    Decision,
    Observation,
    Outcome,
    SeriesState,
    Thresholds,
    compare,
)

D = Decimal
IMMEDIATE = Thresholds(min_delta_abs=D("50"), min_delta_pct=D("2.0"), confirm_checks=1)


def run(state, price, *, available=True, thresholds=IMMEDIATE) -> Decision:
    return compare(state, Observation(price, is_available=available), thresholds)


# ── first sighting ───────────────────────────────────────────────────
def test_first_sighting_records_but_never_alerts():
    """Adding a hotel must not immediately spam its recipient."""
    d = run(None, D("2500"))
    assert d.outcome is Outcome.FIRST_SIGHT
    assert not d.should_notify
    assert d.new_state.last_price == D("2500")


# ── unchanged ────────────────────────────────────────────────────────
def test_identical_price_is_unchanged_and_silent():
    d = run(SeriesState(D("2500"), True), D("2500"))
    assert d.outcome is Outcome.UNCHANGED
    assert not d.should_notify


def test_returning_to_baseline_abandons_a_pending_change():
    """A blip that reverts must not later be confirmed by a second blip."""
    state = SeriesState(D("2500"), True, pending_price=D("2300"), pending_count=1)
    d = run(state, D("2500"))
    assert d.outcome is Outcome.UNCHANGED
    assert d.new_state.pending_price is None
    assert d.new_state.pending_count == 0


# ── the worked example from the requirement ──────────────────────────
def test_requirement_example_2500_to_2300():
    d = run(SeriesState(D("2500"), True), D("2300"))
    assert d.outcome is Outcome.CHANGED
    assert d.direction is ChangeDirection.DECREASE
    assert d.old_price == D("2500")
    assert d.new_price == D("2300")
    assert d.delta == D("-200.00")
    assert d.delta_pct == D("-8.00")
    assert d.should_notify


def test_requirement_example_3000_to_2700():
    d = run(SeriesState(D("3000"), True), D("2700"))
    assert d.delta == D("-300.00")
    assert d.delta_pct == D("-10.00")
    assert d.direction is ChangeDirection.DECREASE


def test_increase_direction():
    d = run(SeriesState(D("2500"), True), D("2900"))
    assert d.direction is ChangeDirection.INCREASE
    assert d.delta == D("400.00")


# ── significance threshold ───────────────────────────────────────────
def test_small_absolute_move_is_ignored():
    """A 20 rupee wobble is not news."""
    d = run(SeriesState(D("2500"), True), D("2520"))
    assert d.outcome is Outcome.INSIGNIFICANT
    assert not d.should_notify


def test_move_must_clear_both_floors():
    """3% of 1000 is only 30 rupees: significant in percent, trivial in money."""
    d = run(SeriesState(D("1000"), True), D("1030"))
    assert d.outcome is Outcome.INSIGNIFICANT


def test_large_absolute_but_tiny_percent_is_ignored():
    """500 rupees off 50,000 is 1%: real money, but not a strategy change."""
    d = run(SeriesState(D("50000"), True), D("50500"))
    assert d.outcome is Outcome.INSIGNIFICANT


def test_zero_thresholds_report_every_move_but_still_debounce():
    """The deployed configuration: DEFAULT_MIN_DELTA_ABS/PCT are both 0.

    Wanted because the 50-rupee AND 2% rule stayed silent on a real 32.50 drop
    (2.8%) -- it cleared the percentage but not the money -- and the baseline
    only resets on a move that clears the rule, so the next comparison began
    from a price the hotel had already stopped charging.

    Blip protection moves entirely to confirm_checks: with the floors at zero
    a one-rupee flicker is "significant", so the ONLY thing keeping it out of
    someone's inbox is having to survive a second consecutive check. That is
    what this pins down -- if the debounce ever regressed, zero floors would
    turn every dynamic-pricing twitch into a notification.
    """
    every = Thresholds(min_delta_abs=D("0"), min_delta_pct=D("0"), confirm_checks=2)
    baseline = SeriesState(D("1153.75"), True)

    # The move that used to be invisible.
    first = run(baseline, D("1121.25"), thresholds=every)
    assert first.outcome is Outcome.PENDING_CONFIRMATION
    assert not first.should_notify

    # Same price again on the next check: now it counts.
    confirmed = run(first.new_state, D("1121.25"), thresholds=every)
    assert confirmed.outcome is Outcome.CHANGED
    assert confirmed.should_notify
    assert confirmed.direction is ChangeDirection.DECREASE
    assert confirmed.delta == D("-32.50")

    # A single rupee is reported too -- but only after it repeats.
    assert run(baseline, D("1152.75"), thresholds=every).outcome is (
        Outcome.PENDING_CONFIRMATION
    )

    # An identical reading is still "no change", not a zero-delta alert.
    assert run(baseline, D("1153.75"), thresholds=every).outcome is Outcome.UNCHANGED


def test_a_blip_that_reverts_stays_quiet_even_with_zero_thresholds():
    """Flicker to a new price and straight back must not notify."""
    every = Thresholds(min_delta_abs=D("0"), min_delta_pct=D("0"), confirm_checks=2)
    baseline = SeriesState(D("2000"), True)

    blip = run(baseline, D("1995"), thresholds=every)
    assert blip.outcome is Outcome.PENDING_CONFIRMATION

    back = run(blip.new_state, D("2000"), thresholds=every)
    assert back.outcome is Outcome.UNCHANGED
    assert not back.should_notify
    assert back.new_state.pending_price is None


def test_insignificant_moves_are_measured_against_the_confirmed_baseline():
    """Slow drift must eventually alert rather than creep past forever.

    Each step is below the threshold, but the comparison is against the last
    CONFIRMED price, so the cumulative move is caught.
    """
    baseline = SeriesState(D("2500"), True)
    assert run(baseline, D("2530")).outcome is Outcome.INSIGNIFICANT
    assert run(baseline, D("2560")).outcome is Outcome.CHANGED  # 60 off 2500 = 2.4%


# ── debounce ─────────────────────────────────────────────────────────
def test_a_single_blip_does_not_alert_when_confirmation_required():
    t = Thresholds(min_delta_abs=D("50"), min_delta_pct=D("2.0"), confirm_checks=2)
    d = run(SeriesState(D("2500"), True), D("2300"), thresholds=t)
    assert d.outcome is Outcome.PENDING_CONFIRMATION
    assert not d.should_notify
    assert d.new_state.last_price == D("2500")  # baseline not yet moved
    assert d.new_state.pending_price == D("2300")


def test_the_same_price_twice_confirms_the_change():
    t = Thresholds(min_delta_abs=D("50"), min_delta_pct=D("2.0"), confirm_checks=2)
    first = run(SeriesState(D("2500"), True), D("2300"), thresholds=t)
    second = compare(first.new_state, Observation(D("2300")), t)
    assert second.outcome is Outcome.CHANGED
    assert second.should_notify
    assert second.new_state.last_price == D("2300")
    assert second.new_state.pending_count == 0


def test_a_different_second_price_restarts_the_debounce():
    """Two different blips are not a confirmation of either one."""
    t = Thresholds(min_delta_abs=D("50"), min_delta_pct=D("2.0"), confirm_checks=2)
    state = SeriesState(D("2500"), True, pending_price=D("2300"), pending_count=1)
    d = run(state, D("2200"), thresholds=t)
    assert d.outcome is Outcome.PENDING_CONFIRMATION
    assert d.new_state.pending_price == D("2200")
    assert d.new_state.pending_count == 1


def test_three_check_confirmation():
    t = Thresholds(min_delta_abs=D("50"), min_delta_pct=D("2.0"), confirm_checks=3)
    s = SeriesState(D("2500"), True)
    for expected in (Outcome.PENDING_CONFIRMATION, Outcome.PENDING_CONFIRMATION):
        d = compare(s, Observation(D("2300")), t)
        assert d.outcome is expected
        s = d.new_state
    assert compare(s, Observation(D("2300")), t).outcome is Outcome.CHANGED


# ── availability: the classic false-alert trap ───────────────────────
def test_sold_out_is_not_a_price_drop_to_zero():
    """The bug this whole branch exists to prevent."""
    d = run(SeriesState(D("2500"), True), None, available=False)
    assert d.outcome is Outcome.BECAME_UNAVAILABLE
    assert d.direction is ChangeDirection.BECAME_UNAVAILABLE
    assert d.new_price is None
    assert d.delta is None  # explicitly NOT -2500
    assert d.should_notify


def test_last_price_is_remembered_while_sold_out():
    """So that when the room returns we can say what it used to cost."""
    d = run(SeriesState(D("2500"), True), None, available=False)
    assert d.new_state.last_price == D("2500")
    assert d.new_state.is_available is False


def test_staying_sold_out_is_silent():
    d = run(SeriesState(D("2500"), False), None, available=False)
    assert d.outcome is Outcome.UNCHANGED
    assert not d.should_notify


def test_room_coming_back_reports_the_price_move_since_it_vanished():
    d = run(SeriesState(D("2500"), False), D("2900"))
    assert d.outcome is Outcome.BECAME_AVAILABLE
    assert d.old_price == D("2500")
    assert d.new_price == D("2900")
    assert d.delta == D("400.00")
    assert d.should_notify
    assert d.new_state.is_available is True


# ── defensive: bad adapter output must never invent a change ─────────
def test_available_but_priceless_is_not_a_change():
    """An adapter bug must produce a gap, never a fabricated price move."""
    d = run(SeriesState(D("2500"), True), None, available=True)
    assert d.outcome is Outcome.UNCHANGED
    assert not d.should_notify
    assert d.new_state.last_price == D("2500")  # baseline preserved


def test_zero_baseline_does_not_divide_by_zero():
    d = run(SeriesState(D("0"), True), D("2500"))
    assert d.delta_pct == D("100.00")
    assert d.outcome is Outcome.CHANGED


# ── rounding ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "old,new,pct",
    [
        (D("2500"), D("2300"), D("-8.00")),
        (D("3000"), D("2700"), D("-10.00")),
        (D("2000"), D("2500"), D("25.00")),
        (D("1999"), D("2499"), D("25.01")),
    ],
)
def test_percentage_rounding(old, new, pct):
    assert run(SeriesState(old, True), new).delta_pct == pct


def test_prices_are_quantised_to_two_decimals():
    d = run(SeriesState(D("2500"), True), D("2300.005"))
    assert d.new_price == D("2300.01")
