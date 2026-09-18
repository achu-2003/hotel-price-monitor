"""A second opinion on where a rate should sit, from a language model.

WHAT THIS IS, AND WHAT IT IS NOT
================================
``repricing.market_figure`` takes the median of the competitors and
``repricing.aim`` moves it by a fixed percentage. That rule is blind by
design: it cannot see that tomorrow is a long weekend, that one competitor
has cut its entry rate three mornings running, or that the owner's room is
the only one of its tier still on sale tonight. This module asks a model to
look at the same night and say where the rate should sit instead.

**It does not decide anything.** It returns a POSITION -- the same
``position_pct`` the owner's rule already has, "0 is match the market, -5 is
sit five per cent under it" -- and that number then goes through
``repricing.aim`` and ``repricing.guard`` exactly as the rule's own does. The
model never sees a rupee figure it can write, never names a target, and
cannot widen a bound. The most an unhinged answer can do is ask for the
extreme of the band, which the step cap then limits to one bounded move.

WHY A POSITION AND NOT A PRICE
==============================
A price carries the market figure, the rounding, the step cap, the floor and
the ceiling all inside one number, and a model that returns one has quietly
taken over all five. A position is the single judgement the rule cannot make
for itself, and leaving the arithmetic where it already lives means the
advisor can be wrong without being dangerous. It also makes the two
comparable: the shadow log holds the rule's position and the model's side by
side, in the same unit, for the same room on the same night.

THE BAND IS NOT ADVICE EITHER
=============================
``max_position_pct`` is a hard clamp applied in this module, before the
number reaches the repricer. A model that answers -60 gets -8 recorded, with
``clamped`` set, and the reason kept. The clamp is deliberately much tighter
than the rule's floor and ceiling: those are the outer wall of where a rate
may ever end up, and this is how far a single night's judgement may reach.

EVERY FAILURE IS A NON-ANSWER
=============================
No network, a timeout, a malformed payload, a refusal, a number that is not a
number -- all of them return an ``Advice`` with ``error`` set and
``position_pct`` of ``None``. Nothing raises. The repricer that called this
carries on with the rule's own proposal, because a rate run that dies because
an API was slow is a worse outcome than one that simply had no second
opinion.

PURE AT THE EDGES
=================
The prompt is built from plain values and the HTTP call goes through an
injected ``complete`` callable, so every branch here is testable without a
network or a key. ``advise`` is the whole surface.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import structlog

log = structlog.get_logger(__name__)

#: What the model is asked to return. ``strict`` schemas refuse anything with
#: a key the schema does not name, which is what stops a chatty answer from
#: arriving as prose wrapped around the number.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["position_pct", "confidence", "rationale", "key_factors"],
    "properties": {
        "position_pct": {
            "type": "number",
            "description": (
                "Where the owner's guest price should sit against its USUAL "
                "level -- the competitors' median times the gap the owner "
                "usually keeps to it -- as a percentage. 0 means the usual "
                "level exactly, -5 means five per cent cheaper than usual, "
                "5 means five per cent dearer."
            ),
        },
        "confidence": {
            "type": "number",
            "description": (
                "How much the evidence supports this position, 0 to 1. Use a "
                "low value when the competitor set is thin, contradictory, or "
                "looks mis-scraped."
            ),
        },
        "rationale": {
            "type": "string",
            "description": (
                "Two or three sentences a hotel owner would accept as a "
                "reason, naming the competitors or the dates that drove it."
            ),
        },
        "key_factors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Three or fewer short phrases, the drivers in order of weight.",
        },
    },
}

SYSTEM = """\
You advise on room pricing for a small hotel in Yelagiri, Tamil Nadu, India.

You are given one room, for one night, with every competitor's entry price
for the same tier of room on that night, and the gap the owner's price
usually keeps to the MEDIAN of those competitors (their room is usually well
above a market that mixes budget chains and resorts -- that premium is the
product, not a mistake). The median times that gap is the room's USUAL
level tonight. You return where the owner's price should sit against that
usual level, as a percentage.

How to think about it:

* The usual level is the anchor. Your job is to say whether tonight is a
  night to sit under it, on it, or above it, and by how much.
* Move away from the usual level only for a reason you can name from the
  data you were given. "The market is at 7,400" is not a reason to move; "the only
  two rooms of this tier still on sale are both above 9,000" is.
* A weekend or a festival night in the hill stations is a reason to sit
  higher; a mid-week night in the monsoon is a reason to sit lower.
* A competitor far outside the rest of the set is more likely a bad reading
  than a real price. Say so in the rationale and lower your confidence rather
  than chasing it.
* A thin set -- two or three competitors -- deserves a position near 0 and a
  low confidence. Do not invent conviction the evidence does not support.
* You do not know how full the hotel is, and you have no booking or occupancy
  data. Never write as though you do.

Be conservative. A position near 0 is the right answer far more often than a
large one, and the owner sees your rationale beside the number.\
"""


@dataclass(frozen=True, slots=True)
class Advice:
    """What the model made of one room on one night.

    ``position_pct`` is ``None`` whenever the answer could not be used, and
    ``error`` then says why in a line short enough for a log and a table cell.
    """

    position_pct: Decimal | None = None
    confidence: float | None = None
    rationale: str | None = None
    key_factors: tuple[str, ...] = ()
    #: Set when the model's number was outside the band and pulled to its edge.
    clamped: str | None = None
    error: str | None = None
    model: str | None = None
    latency_ms: int | None = None

    @property
    def usable(self) -> bool:
        return self.position_pct is not None


@dataclass(frozen=True, slots=True)
class Ask:
    """Everything the model is told about one room on one night.

    Built by the caller from rows it already has, so this module needs no
    database. ``competitors`` is ``(hotel, room, price)`` per rival hotel --
    their ENTRY price for the tier, the same figure the comparison page shows.
    """

    hotel: str
    room_name: str
    tier_label: str
    check_in: date
    our_price: Decimal
    competitors: tuple[tuple[str, str, Decimal], ...]
    #: The rule's own position, so the model can see what it is second-guessing.
    rule_position_pct: Decimal
    #: Our usual price over the median (1.30 = 30% above), from repricing.usual_gaps.
    usual_gap: Decimal | None = None
    #: Our recent prices for this room, oldest first, for movement context.
    our_recent: tuple[Decimal, ...] = ()
    notes: tuple[str, ...] = field(default=())

    def as_prompt(self) -> str:
        lines = [
            f"Hotel: {self.hotel}",
            f"Room: {self.room_name}  (tier: {self.tier_label})",
            f"Night: {self.check_in.isoformat()} ({self.check_in.strftime('%A')})",
            f"Our current guest price: {self.our_price:,.0f}",
            *([f"We usually sit {(self.usual_gap - 1) * 100:+.0f}% against the competitors' median"]
              if self.usual_gap is not None else
              ["There is not enough history to say where we usually sit; take the median itself as the usual level"]),
            "",
            f"Competitor entry prices for this tier ({len(self.competitors)} hotels):",
        ]
        for hotel, room, price in self.competitors:
            lines.append(f"  - {hotel}: {price:,.0f}  ({room})")
        if self.our_recent:
            movement = ", ".join(f"{p:,.0f}" for p in self.our_recent)
            lines += ["", f"Our price for this room over recent checks (oldest first): {movement}"]
        if self.notes:
            lines += ["", "Notes about the data:"] + [f"  - {n}" for n in self.notes]
        lines += [
            "",
            f"The owner's standing rule would sit at {self.rule_position_pct:+.1f}% "
            f"against the usual level.",
            "",
            "Where should this rate sit tonight, and why?",
        ]
        return "\n".join(lines)


def _clamp(value: Decimal, limit: Decimal) -> tuple[Decimal, str | None]:
    if value > limit:
        return limit, f"model asked for {value:+.1f}%, clamped to {limit:+.1f}%"
    if value < -limit:
        return -limit, f"model asked for {value:+.1f}%, clamped to {-limit:+.1f}%"
    return value, None


def parse(payload: str, *, max_position_pct: Decimal) -> Advice:
    """Turn the model's JSON into an ``Advice``, or into an error.

    Separate from the call so the whole of this -- bad JSON, a missing key, a
    string where a number belongs, a number far outside the band -- is
    testable without a network.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        return Advice(error=f"answer was not JSON: {exc}")
    if not isinstance(data, dict):
        return Advice(error="answer was JSON but not an object")

    raw = data.get("position_pct")
    try:
        position = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Advice(error=f"position_pct was not a number: {raw!r}")
    if not position.is_finite():
        return Advice(error=f"position_pct was not finite: {raw!r}")

    position, clamped = _clamp(position, max_position_pct)

    confidence = data.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = None
    else:
        # A confidence outside 0-1 says the model answered a different
        # question than the one asked; the number is still usable, the
        # confidence is not.
        if not 0.0 <= confidence <= 1.0:
            confidence = None

    factors = data.get("key_factors")
    if isinstance(factors, list):
        factors = tuple(str(f)[:120] for f in factors[:3])
    else:
        factors = ()

    rationale = data.get("rationale")
    rationale = str(rationale)[:2000] if rationale is not None else None

    return Advice(
        position_pct=position,
        confidence=confidence,
        rationale=rationale,
        key_factors=factors,
        clamped=clamped,
    )


def advise(ask: Ask, *, complete, model: str, max_position_pct: Decimal) -> Advice:
    """Ask the model where this rate should sit. Never raises.

    ``complete(system, user, schema, model)`` does the HTTP call and returns
    the model's JSON as a string. It is injected so this function can be
    tested, and so the provider can change without this file changing.
    """
    started = time.monotonic()
    try:
        payload = complete(system=SYSTEM, user=ask.as_prompt(), schema=SCHEMA, model=model)
    except Exception as exc:  # noqa: BLE001 -- a failed advisor is never fatal
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning(
            "advisor_call_failed",
            room=ask.room_name, check_in=ask.check_in.isoformat(),
            error=str(exc)[:200], latency_ms=elapsed,
        )
        return Advice(error=f"{type(exc).__name__}: {exc}"[:300], model=model, latency_ms=elapsed)

    elapsed = int((time.monotonic() - started) * 1000)
    parsed = parse(payload, max_position_pct=max_position_pct)
    advice = Advice(
        position_pct=parsed.position_pct,
        confidence=parsed.confidence,
        rationale=parsed.rationale,
        key_factors=parsed.key_factors,
        clamped=parsed.clamped,
        error=parsed.error,
        model=model,
        latency_ms=elapsed,
    )
    if advice.error:
        log.warning("advisor_answer_unusable", room=ask.room_name, error=advice.error)
    return advice
