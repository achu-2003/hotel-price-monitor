"""The price comparison state machine.

Given the last known state of a price series and a freshly observed price,
decide what happened and whether it is worth telling anyone.

Deliberately pure: no database, no clock, no I/O. Every branch is reachable
from a plain unit test, which matters because this is the code that decides
whether a person's phone buzzes.

THREE RULES THAT PREVENT FALSE ALARMS
=====================================
1. **Different booking conditions are never compared.** Enforced upstream by
   ``offer_key``: they are different series, so they never meet here.

2. **Unavailable is not a price of zero.** A sold-out room is a distinct event
   with its own message. Treating it as "price dropped to 0" would produce a
   spectacular false alert.

3. **Confirm before shouting.** A change must clear a significance threshold
   AND persist across ``confirm_checks`` consecutive checks. Dynamic pricing,
   A/B tests and session variance all produce one-off blips; without this the
   alerts become noise and get ignored, which is the real failure mode.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.db.models.enums import ChangeDirection

_CENTS = Decimal("0.01")
_PCT = Decimal("0.01")


class Outcome(StrEnum):
    """What the comparison concluded."""

    FIRST_SIGHT = "first_sight"          # new series — record it, tell nobody
    UNCHANGED = "unchanged"              # identical price
    INSIGNIFICANT = "insignificant"      # moved, but below the alert threshold
    PENDING_CONFIRMATION = "pending"     # moved enough, waiting for it to persist
    CHANGED = "changed"                  # confirmed: notify
    BECAME_UNAVAILABLE = "became_unavailable"
    BECAME_AVAILABLE = "became_available"

    @property
    def is_notifiable(self) -> bool:
        return self in {
            Outcome.CHANGED,
            Outcome.BECAME_UNAVAILABLE,
            Outcome.BECAME_AVAILABLE,
        }


@dataclass(frozen=True, slots=True)
class SeriesState:
    """The stored state of one price series. ``None`` means it does not exist yet."""

    last_price: Decimal | None
    is_available: bool
    pending_price: Decimal | None = None
    pending_count: int = 0


@dataclass(frozen=True, slots=True)
class Observation:
    """What we just saw. ``price`` may be ``None`` when the room is sold out."""

    price: Decimal | None
    is_available: bool = True


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Alert sensitivity.

    A move must clear BOTH the absolute and the percentage floor to count, so
    a 2% wobble on a cheap room and a 20 rupee move on an expensive one are
    both correctly ignored.
    """

    min_delta_abs: Decimal = Decimal("50")
    min_delta_pct: Decimal = Decimal("2.0")
    confirm_checks: int = 2


@dataclass(frozen=True, slots=True)
class Decision:
    """The result. ``new_state`` is what the caller should persist."""

    outcome: Outcome
    new_state: SeriesState
    direction: ChangeDirection | None = None
    old_price: Decimal | None = None
    new_price: Decimal | None = None
    delta: Decimal | None = None
    delta_pct: Decimal | None = None

    @property
    def should_notify(self) -> bool:
        return self.outcome.is_notifiable

    @property
    def should_record_change(self) -> bool:
        """A ``price_changes`` row is written for exactly the notifiable outcomes."""
        return self.outcome.is_notifiable


def _q(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _pct_change(old: Decimal, new: Decimal) -> Decimal:
    """Percentage change, guarding against a zero baseline.

    A stored price of 0 is almost always bad data rather than a free room, so
    treat any move away from it as fully significant rather than dividing by
    zero.
    """
    if old == 0:
        return Decimal("100.00") if new != 0 else Decimal("0.00")
    return ((new - old) / old * 100).quantize(_PCT, rounding=ROUND_HALF_UP)


def _is_significant(delta: Decimal, pct: Decimal, thresholds: Thresholds) -> bool:
    return (
        abs(delta) >= thresholds.min_delta_abs
        and abs(pct) >= thresholds.min_delta_pct
    )


def compare(
    state: SeriesState | None,
    observation: Observation,
    thresholds: Thresholds | None = None,
) -> Decision:
    """Decide what this observation means.

    ``state is None`` for a series being seen for the first time.
    """
    t = thresholds or Thresholds()
    obs_price = _q(observation.price)

    # ── 1. First sighting ────────────────────────────────────────────
    # No baseline exists, so nothing can have "changed". Alerting here would
    # mean every newly added hotel immediately spams its recipient.
    if state is None:
        return Decision(
            outcome=Outcome.FIRST_SIGHT,
            new_state=SeriesState(
                last_price=obs_price,
                is_available=observation.is_available,
            ),
            new_price=obs_price,
        )

    # ── 2. Availability transitions ──────────────────────────────────
    # Checked before price, because a sold-out room has no price to compare
    # and must never be read as a drop to zero.
    if state.is_available and not observation.is_available:
        return Decision(
            outcome=Outcome.BECAME_UNAVAILABLE,
            new_state=SeriesState(
                last_price=state.last_price,  # remembered for when it returns
                is_available=False,
            ),
            direction=ChangeDirection.BECAME_UNAVAILABLE,
            old_price=state.last_price,
            new_price=None,
        )

    if not state.is_available and observation.is_available:
        delta = pct = None
        if state.last_price is not None and obs_price is not None:
            delta = _q(obs_price - state.last_price)
            pct = _pct_change(state.last_price, obs_price)
        return Decision(
            outcome=Outcome.BECAME_AVAILABLE,
            new_state=SeriesState(last_price=obs_price, is_available=True),
            direction=ChangeDirection.BECAME_AVAILABLE,
            old_price=state.last_price,
            new_price=obs_price,
            delta=delta,
            delta_pct=pct,
        )

    # Still unavailable, and still no price: nothing to say.
    if not observation.is_available:
        return Decision(
            outcome=Outcome.UNCHANGED,
            new_state=replace(state, pending_price=None, pending_count=0),
        )

    # ── 3. Both available: compare prices ────────────────────────────
    if obs_price is None or state.last_price is None:
        # Available but priceless is an adapter bug, not a business event.
        # Record the observation; never invent a change from missing data.
        return Decision(
            outcome=Outcome.UNCHANGED,
            new_state=replace(
                state,
                last_price=obs_price if state.last_price is None else state.last_price,
                pending_price=None,
                pending_count=0,
            ),
            new_price=obs_price,
        )

    if obs_price == state.last_price:
        # Back to the baseline: any half-formed pending change is abandoned.
        return Decision(
            outcome=Outcome.UNCHANGED,
            new_state=replace(state, pending_price=None, pending_count=0),
            old_price=state.last_price,
            new_price=obs_price,
        )

    delta = _q(obs_price - state.last_price)
    pct = _pct_change(state.last_price, obs_price)
    assert delta is not None  # obs_price and last_price are both non-None here

    # ── 4. Significance threshold ────────────────────────────────────
    if not _is_significant(delta, pct, t):
        # Deliberately compared against the CONFIRMED baseline rather than the
        # previous observation, so a series of small drifts eventually adds up
        # to a real alert instead of creeping past unnoticed forever.
        return Decision(
            outcome=Outcome.INSIGNIFICANT,
            new_state=replace(state, pending_price=None, pending_count=0),
            old_price=state.last_price,
            new_price=obs_price,
            delta=delta,
            delta_pct=pct,
        )

    # ── 5. Debounce ──────────────────────────────────────────────────
    count = state.pending_count + 1 if state.pending_price == obs_price else 1

    if count < t.confirm_checks:
        return Decision(
            outcome=Outcome.PENDING_CONFIRMATION,
            new_state=replace(state, pending_price=obs_price, pending_count=count),
            old_price=state.last_price,
            new_price=obs_price,
            delta=delta,
            delta_pct=pct,
        )

    # ── 6. Confirmed ─────────────────────────────────────────────────
    return Decision(
        outcome=Outcome.CHANGED,
        new_state=SeriesState(
            last_price=obs_price,
            is_available=True,
            pending_price=None,
            pending_count=0,
        ),
        direction=(
            ChangeDirection.INCREASE if delta > 0 else ChangeDirection.DECREASE
        ),
        old_price=state.last_price,
        new_price=obs_price,
        delta=delta,
        delta_pct=pct,
    )
