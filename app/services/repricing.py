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

THE RULE: KEEP OUR USUAL GAP, FOLLOW THE MARKET'S MOVES
=======================================================
:func:`market_figure` turns those prices into one number: the MEDIAN, since
with ten hotels on a hill one of them at a silly price on a given morning is
normal, and a mean follows the silly price while a median does not.

The median is NOT where our rate belongs. The market is every hotel on the
page, budget chains and resorts together, and the owner's room has sold
well above that mixture for as long as there is history -- that premium is
the product, not an error. A rule that aimed at the median would read the
premium as a mistake and cut, and in automatic mode cut again every run.
So :func:`usual_gaps` measures, per room, where our price has sat against
the median over the last :data:`HISTORY_NIGHTS` nights (the median of
those nightly ratios), and :func:`aim` puts tonight's rate at the same
place against tonight's median. The market moves 10% down, we move 10%
down; it moves up, we move up; it stands still, so do we. A room without
:data:`MIN_HISTORY_NIGHTS` of history gets no proposal: there is nothing to
say what "our place" is.

On top of that, two demand signals (:func:`demand`): the share of the
tier's competitors SOLD OUT tonight, which the median cannot see -- a sold
out hotel drops out of it, so a busy night can even lower the median -- and
an optional weekend lift. ``position_pct`` is the owner's standing lean:
0 keeps the usual gap, -5 sits five per cent cheaper than usual.

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

ONE MOVE PER ROOM PER NIGHT. Once a room's rate has been written for a
night, the rule holds that room until the next night: the step cap then
bounds a whole day, not one run of the automatic half-hourly schedule. A
price the owner types is not held by this (:func:`override`).

PURE
====
No database and no browser: rows in, proposals out. The queries and the
grid are the caller's business (``tasks_repricing``).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
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
    #: No room of the tier is on sale at this hotel tonight; ``price`` is
    #: the last one it showed. See :func:`_night`.
    sold_out: bool = False

    def as_json(self) -> dict:
        return {"hotel": self.hotel, "room": self.room, "price": str(self.price), "note": self.note,
                "sold_out": self.sold_out}


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
    #: What the rule aimed at before any limit -- ``target`` differs from it
    #: when the step cap moved it.
    wanted: Decimal | None = None
    #: Our usual price over the median (1.45 = 45% above it), or ``None``
    #: when there was not enough history to say.
    usual_gap: Decimal | None = None
    #: The demand lift applied, in percent, and what it was made of.
    demand_pct: Decimal = Decimal("0")
    demand_note: str | None = None
    #: Competitor hotels of the tier with no room on sale tonight.
    sold_out: tuple[str, ...] = ()

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
    #: Lift on Friday and Saturday nights. 0 by default: following the
    #: market already carries the weekend, since the competitors raise too.
    weekend_pct: Decimal = Decimal("0")
    #: Lift when EVERY competitor of the tier is sold out; scaled by the
    #: share that is (half sold out, half this).
    sold_out_pct: Decimal = Decimal("10")

    @classmethod
    def from_row(cls, row) -> "Rule":
        return cls(
            position_pct=Decimal(row.position_pct),
            max_step_pct=Decimal(row.max_step_pct),
            floor_pct=Decimal(row.floor_pct),
            ceiling_pct=Decimal(row.ceiling_pct),
            min_competitors=int(row.min_competitors),
            round_to=int(row.round_to),
            weekend_pct=Decimal(row.weekend_pct),
            sold_out_pct=Decimal(row.sold_out_pct),
        )


#: How far back :func:`usual_gaps` looks, and how many of those nights must
#: have both our price and a market before the gap is trusted.
HISTORY_NIGHTS = 14
MIN_HISTORY_NIGHTS = 5


# ---------------------------------------------------------------------------
# THE RULE. See the module docstring: keep our usual gap, follow the market.
# ---------------------------------------------------------------------------


def market_figure(prices: list[Decimal]) -> Decimal:
    """One number for "what the market asks", from every competitor's entry price.

    The median: the middle price when they are lined up. It ignores one
    hotel's outlier the way a mean cannot, and with an even count it is the
    midpoint of the middle two.
    """
    return Decimal(median(prices))


def aim(market: Decimal, rule: Rule, *, gap: Decimal = Decimal("1"),
        demand_pct: Decimal = Decimal("0")) -> Decimal:
    """Where to put our rate, before any limit.

    Tonight's median, times where we usually sit against it (``gap``), leaned
    by the owner's standing position and lifted by tonight's demand.
    """
    wanted = market * gap * (1 + rule.position_pct / 100) * (1 + demand_pct / 100)
    return _round_to(wanted, rule.round_to)


def demand(night: date | None, sold_out: int, total: int, rule: Rule) -> tuple[Decimal, str | None]:
    """The lift for tonight's demand, in percent, and a line saying why.

    ``sold_out`` and ``total`` count the tier's competitor HOTELS: a hotel
    with a room of the tier listed and none of them bookable is sold out.
    """
    lift = Decimal("0")
    why = []
    if sold_out and total and rule.sold_out_pct:
        part = (rule.sold_out_pct * Decimal(sold_out) / total).quantize(Decimal("0.1"))
        lift += part
        why.append(f"{sold_out} of {total} competitors sold out (+{_pct(part)}%)")
    if night is not None and night.weekday() in (4, 5) and rule.weekend_pct:
        lift += rule.weekend_pct
        why.append(f"{night.strftime('%A')} night (+{_pct(rule.weekend_pct)}%)")
    return lift, "; ".join(why) or None


# ---------------------------------------------------------------------------


def _pct(value: Decimal) -> str:
    """A percentage as a person writes it: 10, not 10.00 (the column is Numeric(6, 2))."""
    return f"{Decimal(value).normalize():f}"


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
        capped = f"capped at +{_pct(rule.max_step_pct)}% per step (wanted {wanted:,.0f})"
    elif target < our - step:
        target = _round_to(our - step, rule.round_to)
        capped = f"capped at -{_pct(rule.max_step_pct)}% per step (wanted {wanted:,.0f})"

    low = our * (1 - rule.floor_pct / 100)
    high = our * (1 + rule.ceiling_pct / 100)
    if target < low:
        return target, f"below the floor of {low:,.0f}", capped
    if target > high:
        return target, f"above the ceiling of {high:,.0f}", capped
    return target, None, capped


def _night(rows, own_hotel_id: int):
    """One night's rows, sorted into ours and theirs.

    Returns ``(ours, theirs, sold_out)``: ours is ``room_type_id -> [(series,
    room_name, shown)]``; theirs is ``tier -> hotel_id -> [Competitor]``;
    sold_out is ``tier -> {hotel_id: name}`` for hotels that list the tier
    and have none of it on sale.

    A SOLD-OUT HOTEL STAYS IN THE MARKET, at the last price it showed for
    the night. Dropping it would make a busy night look cheap: when the
    dearest hotel of a tier sells out, the median of the rest FALLS, and a
    rule reading that median cuts on exactly the night it should hold (on
    18 Sep, Thanga Kottai's Deluxe at 8,995 sold out and the median went
    from 3,100 to 2,800). Its price is kept, marked ``sold_out``, and the
    sell-out is counted separately as demand.
    """
    ours: dict[int, list] = {}
    theirs: dict[str, dict[int, list[Competitor]]] = {}
    last_known: dict[str, dict[int, list[Competitor]]] = {}
    listed: dict[str, dict[int, str]] = {}

    for series, hotel, room_name in rows:
        shown = displayed_price(series, False)
        if hotel.id == own_hotel_id:
            ours.setdefault(series.room_type_id, []).append((series, room_name, shown))
            continue
        tier = classify(room_name)
        listed.setdefault(tier, {})[hotel.id] = hotel.name
        if shown.amount is None:
            continue
        if not series.is_available:
            last_known.setdefault(tier, {}).setdefault(hotel.id, []).append(
                Competitor(hotel=hotel.name, room=room_name, price=shown.amount, note=shown.note, sold_out=True)
            )
            continue
        theirs.setdefault(tier, {}).setdefault(hotel.id, []).append(
            Competitor(hotel=hotel.name, room=room_name, price=shown.amount, note=shown.note)
        )
    sold_out = {
        tier: {hid: name for hid, name in hotels.items() if hid not in theirs.get(tier, {})}
        for tier, hotels in listed.items()
    }
    for tier, hotels in sold_out.items():
        for hid in hotels:
            if hid in last_known.get(tier, {}):
                theirs.setdefault(tier, {})[hid] = last_known[tier][hid]
    return ours, theirs, sold_out


def _our_entry(entries) -> tuple[Decimal | None, str | None]:
    on_sale = [(s, n, p) for s, n, p in entries if s.is_available and p.amount is not None]
    price = min((p.amount for _, _, p in on_sale), default=None)
    note = next((p.note for _, _, p in on_sale if p.amount == price), None)
    return price, note


def _entry_prices(theirs_in_tier: dict[int, list[Competitor]]) -> tuple[Competitor, ...]:
    """Each hotel's entry price for the tier: its cheapest room of it on sale."""
    return tuple(sorted((min(rooms, key=lambda c: c.price) for rooms in theirs_in_tier.values()),
                        key=lambda c: c.price))


def usual_gaps(rows, *, own_hotel_id: int, min_competitors: int = 2) -> dict[int, tuple[Decimal, int]]:
    """Where each of our rooms usually sits against the median: ``room -> (gap, nights)``.

    ``rows`` are the matrix query's ``(series, hotel, room_name)`` over past
    nights (one-night stays), any number of nights mixed; they are grouped
    by ``series.check_in`` here. For every night with our price on sale and
    at least ``min_competitors`` in the tier, the ratio of our price to the
    median; the gap is the median of those ratios, so one odd night does not
    move it. Rooms with fewer than :data:`MIN_HISTORY_NIGHTS` such nights
    are left out.
    """
    by_night: dict[object, list] = {}
    for row in rows:
        by_night.setdefault(row[0].check_in, []).append(row)

    ratios: dict[int, list[Decimal]] = {}
    for night_rows in by_night.values():
        ours, theirs, _ = _night(night_rows, own_hotel_id)
        for room_type_id, entries in ours.items():
            tier = classify(entries[0][1])
            if tier == OTHER:
                continue
            our_price, _ = _our_entry(entries)
            competitors = _entry_prices(theirs.get(tier, {}))
            if our_price is None or len(competitors) < min_competitors:
                continue
            market = market_figure([c.price for c in competitors])
            if market:
                ratios.setdefault(room_type_id, []).append(our_price / market)

    return {
        room_type_id: (Decimal(median(values)).quantize(Decimal("0.0001")), len(values))
        for room_type_id, values in ratios.items()
        if len(values) >= MIN_HISTORY_NIGHTS
    }


def propose(rows, *, own_hotel_id: int, rule: Rule,
            room_type_ids: set[int] | None = None,
            usual: dict[int, tuple[Decimal, int]] | None = None,
            night: date | None = None,
            moved_today: set[int] | frozenset[int] = frozenset()) -> list[Proposal]:
    """Rows from the matrix query in, one proposal per room of the owner's out.

    ``rows`` are ``(series, hotel, room_name)`` for one night and occupancy,
    every hotel of the account. ``room_type_ids`` restricts which of the
    owner's rooms get a proposal (the mapped ones). ``usual`` is
    :func:`usual_gaps` over the nights before; ``None`` means "no history
    was looked up" and aims at the median itself (gap 1), which only a test
    wants -- a room missing from a dict that WAS passed is held. ``night``
    is the date, for the weekend lift. ``moved_today`` is the rooms already
    written for this night, held until the next.
    """
    ours, theirs, sold_out = _night(rows, own_hotel_id)

    proposals = []
    for room_type_id, entries in ours.items():
        if room_type_ids is not None and room_type_id not in room_type_ids:
            continue
        room_name = entries[0][1]
        tier = classify(room_name)
        our_price, our_note = _our_entry(entries)
        competitors = _entry_prices(theirs.get(tier, {}))
        gone = tuple(sorted(sold_out.get(tier, {}).values()))

        base = dict(
            room_type_id=room_type_id, room_name=room_name, tier=tier,
            tier_label=label_for(tier), our_price=our_price, our_note=our_note,
            competitors=competitors, sold_out=gone,
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
        if usual is None:
            gap = Decimal("1")
        elif room_type_id in usual:
            gap = usual[room_type_id][0]
        else:
            proposals.append(Proposal(**base, market=market, target=None,
                                      held=f"fewer than {MIN_HISTORY_NIGHTS} nights of history, "
                                           f"so there is no usual gap to keep"))
            continue
        lift, why = demand(night, len(gone), len({c.hotel for c in competitors} | set(gone)), rule)
        wanted = aim(market, rule, gap=gap, demand_pct=lift)
        target, held, capped = guard(our_price, wanted, rule)
        if held is None and room_type_id in moved_today:
            held = "already moved once tonight; the next move is tomorrow"
        proposals.append(Proposal(**base, market=market, target=target, held=held, capped=capped,
                                  wanted=wanted, usual_gap=gap if usual is not None else None,
                                  demand_pct=lift, demand_note=why))

    proposals.sort(key=lambda p: (p.tier_label, p.room_name))
    return proposals


def override(proposal: Proposal, price: Decimal) -> Proposal:
    """The proposal with the owner's own guest price in place of the rule's.

    The owner typed the number, so the rule's step cap and percentage limits
    do not move or hold it -- they guard against a bad market reading, and
    this is not one. A hold the rule placed for lack of a market is lifted
    for the same reason. What stays is everything the number still has to
    pass on its way into RMS: a price of ours to take the ratio from, and
    the per-room rupee floor and ceiling (:func:`rms_bounds`), checked by
    the caller once the RMS rate is known.
    """
    if proposal.our_price is None:
        return replace(proposal, held="no price of ours on sale tonight to scale your price from")
    was = f"the rule proposed {proposal.target:,.0f}" if proposal.target is not None else "the rule had no proposal"
    return replace(proposal, target=price, held=None, capped=f"your price ({was})")


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


def same_share(current: Decimal, before: Decimal, after: Decimal, *, round_to: int) -> Decimal:
    """Another channel's rate moved by the share the main channel's moved.

    Booking.com 8,500 -> 7,650 is -10%; a Goibibo 9,000 becomes 8,100. Each
    channel keeps the difference the owner set between them, in proportion.
    This is only the SUGGESTION the page fills in -- another channel is
    written only when the owner ticks it, and then with the number the box
    shows.
    """
    if not before:
        raise ValueError("the main channel's rate is zero; no share to move by")
    return _round_to(current * after / before, round_to)


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
