"""From "where does our rate sit" to "what should it be", for one night.

The comparison page answers the first question. This module answers the
second, room by room, and hands the answer to the RMS driver to type in.

WHAT THE MARKET IS
==================
Every competitor on the comparison page, not a chosen three. For each of the
owner's rooms the tier is read off its name (``room_category.classify``, the
same grouping the comparison page uses, so the two pages cannot disagree
about who is being compared with whom), and the market is every other
hotel's ENTRY price in that tier -- their cheapest bookable room of that
kind, one figure per hotel, exactly the cell the comparison page shows.

THE RULE, AND WHERE TO CHANGE IT
================================
:func:`market_figure` turns those prices into one number, and :func:`aim`
turns that number into a target for the owner's rate. Today the figure is
the MEDIAN: with ten hotels on a hill, one of them at a silly price on a
given morning is normal, and a mean follows the silly price while a median
does not. The target is the median moved by ``position_pct`` -- 0 is "match
the market", -5 is "sit five per cent under it" -- and rounded to
``round_to`` so it reads like a rate. The owner has a better rule in mind;
when it arrives it replaces these two functions and nothing else.

ONE PRICE PER ROOM, SO ONE DECISION PER ROOM
============================================
The sites publish one price for a room -- the cheapest plan it is sold on,
usually room only -- so that is what is compared and that is what the target
is: the ENTRY (EP, room-only) rate. The CP and MAP rates are not decided
separately, because nothing on the market speaks to them; they move with the
EP rate, keeping the meal supplement RMS already has between the plans
(today CLASSIC's CP is EP + 500 and MAP is EP + 3,500, and after a move they
still are). See :func:`to_rms`.

GUEST PRICE IS NOT THE RMS RATE
===============================
The figure the sites show is what a guest pays before tax; the figure typed
into RMS is the base rate, which the channel then discounts (Booking.com
shows the owner's 7,500 as 5,610 under a 25% deal). The two are related by
a ratio the system does not know in advance and does not need to: it reads
the RMS grid and the site on the same morning and takes the ratio between
them, so a target guest price becomes an RMS rate by the same factor the
channel is applying today. If the deal changes, the ratio changes with it.

THE LIMITS ARE NOT ADVICE
=========================
A proposal outside the floor or ceiling is HELD -- shown, with the reason,
and not written, in either mode. A proposal further than ``max_step_pct``
from today's price is capped at that step and written capped, with a note
saying so: a competitor's bad reading then costs one bounded step, not a
cliff. Fewer than ``min_competitors`` and there is no proposal at all.

PURE
====
No database and no browser: rows in, proposals out. The queries and the
grid are the caller's business (``tasks_repricing``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from app.services.price_display import displayed_price
from app.services.room_category import OTHER, classify, label_for


@dataclass(frozen=True, slots=True)
class Competitor:
    hotel: str
    room: str
    price: Decimal
    #: The tax-basis marker from price_display, where the site published the
    #: other component and this one was derived. Carried so the audit row
    #: can say the market figure leaned on an approximate price.
    note: str | None = None

    def as_json(self) -> dict:
        return {"hotel": self.hotel, "room": self.room, "price": str(self.price), "note": self.note}


@dataclass(frozen=True, slots=True)
class Proposal:
    """One of the owner's rooms, and what the rule makes of its position."""

    room_type_id: int
    room_name: str
    tier: str
    tier_label: str
    #: What the owner's room sells for today on the channel (guest price,
    #: before tax), and the marker if that had to be derived.
    our_price: Decimal | None
    our_note: str | None
    competitors: tuple[Competitor, ...]
    market: Decimal | None
    #: Where the rule wants the guest price, after the limits. ``None`` when
    #: there is nothing to say (no price of ours, too few competitors).
    target: Decimal | None
    #: Why nothing may be written, when that is the case.
    held: str | None = None
    #: A limit that changed the number without holding it (the step cap).
    capped: str | None = None

    @property
    def change_pct(self) -> float | None:
        if self.target is None or not self.our_price:
            return None
        return float((self.target - self.our_price) / self.our_price * 100)

    @property
    def actionable(self) -> bool:
        return self.target is not None and self.held is None


@dataclass(frozen=True, slots=True)
class Rule:
    """The settings the rule reads, detached from the ORM row."""

    position_pct: Decimal = Decimal("0")
    max_step_pct: Decimal = Decimal("10")
    floor_pct: Decimal = Decimal("30")
    ceiling_pct: Decimal = Decimal("50")
    min_competitors: int = 2
    round_to: int = 10

    @classmethod
    def from_row(cls, row) -> "Rule":
        return cls(
            position_pct=Decimal(row.position_pct),
            max_step_pct=Decimal(row.max_step_pct),
            floor_pct=Decimal(row.floor_pct),
            ceiling_pct=Decimal(row.ceiling_pct),
            min_competitors=int(row.min_competitors),
            round_to=int(row.round_to),
        )


# ---------------------------------------------------------------------------
# THE RULE. Replace these two functions when the owner's formula arrives.
# ---------------------------------------------------------------------------


def market_figure(prices: list[Decimal]) -> Decimal:
    """One number for "what the market asks", from every competitor's entry price.

    The median: the middle price when they are lined up. It ignores one
    hotel's outlier the way a mean cannot, and with an even count it is the
    midpoint of the middle two.
    """
    return Decimal(median(prices))


def aim(market: Decimal, rule: Rule) -> Decimal:
    """Where to put our rate against that number, before any limit."""
    return _round_to(market * (1 + rule.position_pct / 100), rule.round_to)


# ---------------------------------------------------------------------------


def _round_to(amount: Decimal, step: int) -> Decimal:
    if step <= 1:
        return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (amount / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def guard(our: Decimal, wanted: Decimal, rule: Rule) -> tuple[Decimal, str | None, str | None]:
    """Apply the percentage limits. Returns ``(target, held_reason, capped_note)``.

    The step cap moves the number; the floor and ceiling hold it. A number
    that is capped AND then found outside the bounds is held, because the
    bounds are the outer wall and the step is only how fast one may walk
    towards it. These are the limits on the GUEST price; the per-room
    rupee bounds are on the RMS rate and are checked by :func:`rms_bounds`
    once that number is known.
    """
    target = wanted
    capped = None
    step = our * rule.max_step_pct / 100
    if target > our + step:
        target = _round_to(our + step, rule.round_to)
        capped = f"capped at +{rule.max_step_pct:g}% per step (wanted {wanted:,.0f})"
    elif target < our - step:
        target = _round_to(our - step, rule.round_to)
        capped = f"capped at -{rule.max_step_pct:g}% per step (wanted {wanted:,.0f})"

    low = our * (1 - rule.floor_pct / 100)
    high = our * (1 + rule.ceiling_pct / 100)
    if target < low:
        return target, f"below the floor of {low:,.0f}", capped
    if target > high:
        return target, f"above the ceiling of {high:,.0f}", capped
    return target, None, capped


def propose(rows, *, own_hotel_id: int, rule: Rule,
            room_type_ids: set[int] | None = None) -> list[Proposal]:
    """Rows from the matrix query in, one proposal per room of the owner's out.

    ``rows`` are ``(series, hotel, room_name)`` for one night and occupancy,
    every hotel of the account. ``room_type_ids`` restricts which of the
    owner's rooms get a proposal (the mapped ones).
    """
    ours: dict[int, list] = {}
    theirs: dict[str, dict[int, list[Competitor]]] = {}
    hotel_names: dict[int, str] = {}

    for series, hotel, room_name in rows:
        hotel_names[hotel.id] = hotel.name
        shown = displayed_price(series, False)
        if hotel.id == own_hotel_id:
            ours.setdefault(series.room_type_id, []).append((series, room_name, shown))
            continue
        if not series.is_available or shown.amount is None:
            continue
        tier = classify(room_name)
        theirs.setdefault(tier, {}).setdefault(hotel.id, []).append(
            Competitor(hotel=hotel.name, room=room_name, price=shown.amount, note=shown.note)
        )

    proposals = []
    for room_type_id, entries in ours.items():
        if room_type_ids is not None and room_type_id not in room_type_ids:
            continue
        room_name = entries[0][1]
        tier = classify(room_name)
        on_sale = [(s, n, p) for s, n, p in entries if s.is_available and p.amount is not None]
        our_price = min((p.amount for _, _, p in on_sale), default=None)
        our_note = next((p.note for _, _, p in on_sale if p.amount == our_price), None)

        # Their entry price per hotel: the cheapest room of the tier on sale.
        competitors = tuple(
            sorted(
                (min(rooms, key=lambda c: c.price) for rooms in theirs.get(tier, {}).values()),
                key=lambda c: c.price,
            )
        )

        base = dict(
            room_type_id=room_type_id, room_name=room_name, tier=tier,
            tier_label=label_for(tier), our_price=our_price, our_note=our_note,
            competitors=competitors,
        )
        if tier == OTHER:
            proposals.append(Proposal(**base, market=None, target=None,
                                      held="the room's name states no tier, so it has no market"))
            continue
        if our_price is None:
            proposals.append(Proposal(**base, market=None, target=None,
                                      held="no price of ours on sale tonight to move from"))
            continue
        if len(competitors) < rule.min_competitors:
            proposals.append(Proposal(**base, market=None, target=None,
                                      held=f"only {len(competitors)} competitor(s) price this tier; "
                                           f"the rule needs {rule.min_competitors}"))
            continue

        market = market_figure([c.price for c in competitors])
        wanted = aim(market, rule)
        target, held, capped = guard(our_price, wanted, rule)
        proposals.append(Proposal(**base, market=market, target=target, held=held, capped=capped))

    proposals.sort(key=lambda p: (p.tier_label, p.room_name))
    return proposals


def to_rms(target_guest: Decimal, our_guest: Decimal, current_rms: Decimal, *, round_to: int) -> Decimal:
    """The RMS rate that would make the channel show ``target_guest``.

    By the ratio the channel is applying today: RMS shows 7,500 where the
    site shows 5,610, so a target of 6,000 on the site is 6,000 × 7,500/5,610
    in RMS. Read both on the same morning, so the deal in force is the deal
    in the ratio.
    """
    if not our_guest:
        raise ValueError("our guest price is zero; no ratio to scale by")
    return _round_to(target_guest * current_rms / our_guest, round_to)


def follow(proposed_ep: Decimal, current_ep: Decimal, current_plan: Decimal, *, round_to: int) -> Decimal:
    """A CP or MAP rate after the EP moved: the same supplement over the new EP."""
    return _round_to(proposed_ep + (current_plan - current_ep), round_to)


def rms_bounds(amount: Decimal, *, floor: Decimal | None, ceiling: Decimal | None) -> str | None:
    """Why an RMS rate may not be written, or ``None`` when it may.

    The owner's per-room floor and ceiling are in the numbers they see on
    the RMS grid, so they are checked on the RMS rate, after the conversion
    -- a floor of 7,500 means the CLASSIC EP cell never reads below 7,500.
    """
    if floor is not None and amount < floor:
        return f"below this room's RMS floor of {floor:,.0f}"
    if ceiling is not None and amount > ceiling:
        return f"above this room's RMS ceiling of {ceiling:,.0f}"
    return None
