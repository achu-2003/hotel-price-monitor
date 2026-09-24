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

ONE MOVE PER ROOM PER NIGHT -- UNDER THE MEDIAN RULE ONLY. Once a room's
rate has been written for a night the median rule holds that room until the
next night, so the step cap bounds a whole day rather than one run of the
half-hourly schedule. That is right for an AIM at a figure that wobbles: a
room free to chase the median all evening would walk on noise.

A BENCHMARK FOLLOW IS NOT HELD, because it is not an aim. "A hundred under
Sterling" is an instruction about one named hotel, and if they move at four
o'clock a rule that answers "tomorrow" is not the rule the owner set -- it
leaves us hundreds of rupees off their price for the rest of the day. The
runaway that the lock was imagined to prevent is prevented by the things
that were always doing it: the step cap bounds any one move, the floor and
ceiling hold a number outside them, the room's rupee bounds are checked on
the RMS rate, and a rate equal to the cell's is recorded unchanged rather
than written -- so a run half an hour later does nothing at all unless
their price actually moved.

A price the owner types is held by none of it (:func:`override`).

PURE
====
No database and no browser: rows in, proposals out. The queries and the
grid are the caller's business (``tasks_repricing``).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from statistics import median

from app.services.price_display import displayed_price
from app.services.room_matching import normalize_room_name
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
    #: WHICH SITE said so. A hotel can be tracked on more than one (Sterling
    #: is on Booking.com and on its own booking engine, and the two differ by
    #: hundreds of rupees), and a comparison that takes whichever is cheaper
    #: is comparing our Booking.com rate against their direct rate. The
    #: median does not care -- it is the market's level either way -- but
    #: :class:`Benchmark` does, because that is the whole of its input.
    source_id: int | None = None

    def as_json(self) -> dict:
        return {"hotel": self.hotel, "room": self.room, "price": str(self.price), "note": self.note,
                "sold_out": self.sold_out, "source_id": self.source_id}


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
    #: The one hotel this proposal was priced against, when the owner prices
    #: off a single competitor rather than the median. ``market`` is then
    #: THAT hotel's entry price, not a median, and the page says so. Set
    #: even when the benchmark had nothing to price against, so the held
    #: reason can name it.
    benchmark: str | None = None
    #: The exact competitor row the target was computed from -- their room,
    #: their price, the site it came from. Kept so that everything else
    #: which needs to know what we priced against (the page, the advisor's
    #: prompt) reads the rule's own choice instead of repeating the
    #: selection and risking a different answer.
    benchmark_entry: Competitor | None = None
    #: The board ``our_price`` is quoted on. Normally the one the rule was
    #: told to use, but a room the site sells only room-only falls back to
    #: what it does sell, and then this is what says so -- and what the page
    #: reads to pick the RMS row a typed price would be written to.
    board: str | None = None
    #: ``target`` IS THE NUMBER, not an aim at it. True for the benchmark
    #: follow, where the target is a hundred rupees under a named hotel, and
    #: for a price the owner typed. Both are instructions, and the rounding
    #: that makes a median's approximate aim read nicely is, on an
    #: instruction, an error: see :func:`to_rms`, which is the last place
    #: the number can be lost.
    exact: bool = False

    @property
    def change_pct(self) -> float | None:
        if self.target is None or not self.our_price:
            return None
        return float((self.target - self.our_price) / self.our_price * 100)

    @property
    def short_by(self) -> Decimal | None:
        """How far a capped proposal falls SHORT of the gap it was set to keep.

        A CAPPED FOLLOW CAN LAND ON THE WRONG SIDE OF THE HOTEL IT FOLLOWS,
        and when it does the page must not call it well.

        Tonight's Standard Double: ours 9,913, Sterling 4,636, so the rule
        wants 4,536 and the 50% step allows 4,960. That move is worth making
        -- it is 5,000 rupees in the right direction -- but 4,960 is 324
        ABOVE the hotel we exist to sit a hundred below, and the row said
        "will apply" in green with the cap noted in grey beside it. The one
        fact the owner needs, that tonight the rule does not achieve its
        gap, was the one thing not on the row.

        ``None`` when the gap is kept, so the caller can simply test it.
        Only meaningful for a benchmark follow: the median rule has no fixed
        gap to fall short of.
        """
        if self.benchmark is None or self.target is None or self.wanted is None:
            return None
        return self.target - self.wanted if self.target > self.wanted else None

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


@dataclass(frozen=True, slots=True)
class Benchmark:
    """Price off ONE named competitor, a fixed number of rupees under them.

    The owner's actual rule, in their words: "we have set the price hundred
    rupees less". Sterling moves 1,000 to 1,300 and we set 1,200; they drop
    to 1,200 and we set 1,100. It is a FOLLOW, not an aim -- which is why
    none of the median machinery applies to it (see :func:`propose`).

    ``undercut`` is rupees, not a percentage, and that is the point: a
    percentage of a moving price is a gap that changes every time they move,
    and the owner's gap does not.

    ``with_tax`` says WHICH price the gap is measured on, and it is not a
    presentation choice. Booking.com published 361 of tax on our 6,358 and
    163 on Sterling's 3,256 -- 5.68% against 5.01% -- so a hundred rupees
    under them before tax is NOT a hundred rupees under them on the line the
    guest reads. The owner competes on the figure the guest compares, so
    that is the figure the gap is taken on, and the rate we write is worked
    back from it.
    """

    hotel_id: int
    hotel: str
    undercut: Decimal = Decimal("100")
    with_tax: bool = True
    #: The board both sides are quoted on -- one of ``services.meal_plan``'s
    #: plans, or None to take whatever each room's cheapest rate includes.
    #:
    #: It matters more than it looks. Booking.com sells the same room on two
    #: or three plans, and the supplements are not alike: on 24 Sep adding
    #: breakfast cost ASG 374 and Sterling 1,350. Comparing our room-only
    #: rate against their breakfast one reported us 1,269 dearer than we
    #: were, and comparing like with like closed a gap of 2,015 to 346.
    meal_plan: str | None = None
    #: The board to use for a room the chosen one does not reach. Booking.com
    #: sells ASG's Standard Double room-only and nothing else, so pinning
    #: breakfast left that room with no comparison at all -- and a room the
    #: owner competes with every night is not a room to leave unpriced.
    #:
    #: OUR SIDE ONLY. The benchmark's room is still read on ``meal_plan``:
    #: the owner prices against Sterling's breakfast rate whatever board our
    #: room happens to be sold on. It used to drop both sides together, which
    #: compared ASG's Standard Double against Sterling's Mountain View
    #: room-only rate -- not the figure the owner prices against.
    fallback_board: str | None = None
    #: WHICH room of theirs each room of ours competes with:
    #: ``{our room_type_id: their room name}``. Empty keeps the tier
    #: matching the median rule uses.
    #:
    #: NAMED, because no classifier will ever pair these. The owner's rooms
    #: are "Deluxe Double Room" and "Standard Double Room"; the rooms they
    #: actually lose guests to are Sterling's "Classic room" and "Mountain
    #: View Classic Room". The tier reader puts those four in three
    #: different tiers and is right to -- it reads names, and these names
    #: disagree about which rooms compete. Only the owner knows.
    #:
    #: A room of ours that is not a key gets no proposal: it was not paired,
    #: so nothing has been said about what it competes with, and inventing a
    #: comparison is how a rate moves for a reason nobody chose.
    room_pairs: Mapping[int, str] = field(default_factory=dict)


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


def _round_within(amount: Decimal, step: int, toward: Decimal) -> Decimal:
    """Round ``amount`` to a multiple of ``step``, never further from ``toward``.

    THE NEAREST MULTIPLE CAN OVERSHOOT THE LIMIT THAT PRODUCED IT. A room at
    6,719 with a 30% cap may go to 4,703.3; rounded to the nearest ten that
    is 4,700, which is three rupees past the cap -- and where the floor is
    the same 30%, three rupees is the difference between a move and a
    refusal. The rate was held for being "below the floor of 4,703" while
    showing 4,700, which reads as arithmetic nobody can argue with and is
    really just a rounding artefact.

    So a capped move rounds back towards today's price. It gives up at most
    ``step`` rupees of the move and can never breach the bound it was
    measured against.
    """
    if step <= 1:
        return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    rounding = ROUND_CEILING if amount < toward else ROUND_FLOOR
    return (amount / step).quantize(Decimal("1"), rounding=rounding) * step


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
        target = _round_within(our + step, rule.round_to, our)
        capped = f"capped at +{_pct(rule.max_step_pct)}% per step (wanted {wanted:,.0f})"
    elif target < our - step:
        target = _round_within(our - step, rule.round_to, our)
        capped = f"capped at -{_pct(rule.max_step_pct)}% per step (wanted {wanted:,.0f})"

    low = our * (1 - rule.floor_pct / 100)
    high = our * (1 + rule.ceiling_pct / 100)
    if target < low:
        return target, f"below the floor of {low:,.0f}", capped
    if target > high:
        return target, f"above the ceiling of {high:,.0f}", capped
    return target, None, capped


def _night(rows, own_hotel_id: int, with_tax: bool = False, meal_plan: str | None = None):
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
        # BOTH SIDES ON ONE BOARD. Dropped here rather than after the entry
        # price is chosen, because the cheapest rate of a room is usually the
        # room-only one and picking it first would mean the filter never saw
        # the breakfast rate it was asked for.
        #
        # A rate whose board could not be read is dropped too. It is not
        # evidence of being the board wanted, and letting it through is how a
        # room-only rate gets compared against a rival's breakfast one.
        if meal_plan is not None and series.meal_plan != meal_plan:
            continue
        shown = displayed_price(series, with_tax)
        if hotel.id == own_hotel_id:
            ours.setdefault(series.room_type_id, []).append((series, room_name, shown))
            continue
        tier = classify(room_name)
        listed.setdefault(tier, {})[hotel.id] = hotel.name
        if shown.amount is None:
            continue
        if not series.is_available:
            last_known.setdefault(tier, {}).setdefault(hotel.id, []).append(
                Competitor(hotel=hotel.name, room=room_name, price=shown.amount, note=shown.note,
                           sold_out=True, source_id=series.source_id)
            )
            continue
        theirs.setdefault(tier, {}).setdefault(hotel.id, []).append(
            Competitor(hotel=hotel.name, room=room_name, price=shown.amount, note=shown.note,
                       source_id=series.source_id)
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


def _our_entry(entries) -> tuple[Decimal | None, str | None, int | None]:
    """Our entry price for the room, the tax marker, and the site it came from.

    The source travels with it because :class:`Benchmark` compares like for
    like: the competitor's price on the SAME site as ours, not their cheapest
    across every site they are listed on.
    """
    on_sale = [(s, n, p) for s, n, p in entries if s.is_available and p.amount is not None]
    price = min((p.amount for _, _, p in on_sale), default=None)
    note = next((p.note for _, _, p in on_sale if p.amount == price), None)
    source_id = next((s.source_id for s, _, p in on_sale if p.amount == price), None)
    return price, note, source_id


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
            our_price, _, _ = _our_entry(entries)
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


def _benchmark_entry(theirs_in_tier: dict[int, list[Competitor]], hotel_id: int,
                     source_id: int | None) -> Competitor | None:
    """The benchmark's cheapest BOOKABLE room of the tier ON OUR SITE, or nothing.

    THE SITE MATTERS HERE AND NOWHERE ELSE. Sterling is tracked twice --
    Booking.com and its own booking engine -- and on one night those read
    2,931 and 2,460 for the same room. To the median that is a competitor
    priced somewhere around 2,700 and either figure would do. To a rule that
    sets our rate a fixed sum under theirs it is the entire input, and taking
    whichever happens to be cheaper prices our Booking.com room against their
    direct-booking discount. So the comparison is pinned to the site our own
    price came from, and a night that site did not report theirs produces no
    proposal rather than a cross-site one.

    Sold-out rooms are excluded, which is a departure from the median: that
    deliberately keeps a full hotel in at its last price so a busy night does
    not read as a cheap one. Here it would be the whole input, and the rate
    we wrote would sit under a price that has stopped existing.
    """
    return _cheapest_of(theirs_in_tier.get(hotel_id) or [], source_id)


def _cheapest_of(rooms: list[Competitor], source_id: int | None) -> Competitor | None:
    on_sale = [c for c in rooms
               if not c.sold_out and (source_id is None or c.source_id == source_id)]
    if not on_sale:
        return None
    return min(on_sale, key=lambda c: c.price)


def _fallback_rate(rows, own_hotel_id: int, room_type_id: int,
                   with_tax: bool) -> tuple[Decimal | None, str | None, str | None]:
    """Our cheapest bookable rate for one room, on whatever board sells it.

    Only for rooms the chosen board does not reach. It is deliberately NOT
    fed to the comparison -- a room-only rate set against a rival's
    breakfast one is the mistake this whole board business exists to stop.
    It is the ratio a typed price needs, and the label that says which rate
    is on screen.
    """
    best: tuple[Decimal | None, str | None, str | None] = (None, None, None)
    for series, hotel, _name in rows:
        if hotel.id != own_hotel_id or series.room_type_id != room_type_id:
            continue
        if not series.is_available:
            continue
        shown = displayed_price(series, with_tax)
        if shown.amount is None:
            continue
        if best[0] is None or shown.amount < best[0]:
            best = (shown.amount, shown.note, series.meal_plan)
    return best


def _benchmark_room(theirs: dict, hotel_id: int, source_id: int | None,
                    room_name: str) -> Competitor | None:
    """The benchmark's NAMED room, on our site, bookable tonight.

    Matched through ``normalize_room_name`` rather than on the string, for
    the same reason the alias table is: the owner picks the name off a list
    that came from the site, and the site then writes "Classic room" one
    week and "Classic Room" the next. Token order is normalised away too, so
    a rename to "Mountain View Classic" still finds it.

    Searched across every tier, because the tier is exactly what this
    pairing exists to overrule -- their "Classic room" and our "Deluxe
    Double Room" are competitors whatever the classifier makes of the two
    names.

    ``None`` when that room is not on sale tonight, on our site, on the
    chosen board. The caller holds the proposal and says so; it does not
    slide across to whichever other room of theirs happens to be cheapest,
    because the owner named this one.
    """
    key = normalize_room_name(room_name)
    if not key:
        return None
    rooms = [c for by_hotel in theirs.values()
             for c in (by_hotel.get(hotel_id) or [])
             if normalize_room_name(c.room) == key]
    return _cheapest_of(rooms, source_id)


def usual_gaps_against(rows, *, own_hotel_id: int,
                       benchmark_hotel_id: int) -> dict[int, tuple[Decimal, int]]:
    """Where each of our rooms usually sits against ONE named hotel.

    The same measurement :func:`usual_gaps` makes, with the market narrowed
    to a single competitor: the median of one price is that price, so the
    ratio it produces is our price over theirs, night by night.

    ``min_competitors=1`` here is not the guard being waived. That guard
    exists to refuse a market of one hotel that nobody chose; this is a
    market of one hotel the owner chose on purpose, and refusing it would
    mean the setting could be set and never take effect.
    :data:`MIN_HISTORY_NIGHTS` still applies, so a benchmark added this week
    has no gap yet and says so rather than inventing one.

    NOT INTERCHANGEABLE WITH :func:`usual_gaps`. Our premium over a budget
    chain and our premium over the median are different numbers for the same
    room, and only the advisor's prompt reads this one -- the rule's
    arithmetic keeps using the median's.
    """
    return usual_gaps(
        [row for row in rows if row[1].id in (own_hotel_id, benchmark_hotel_id)],
        own_hotel_id=own_hotel_id,
        min_competitors=1,
    )


def benchmark_history(rows, *, own_hotel_id: int, benchmark_hotel_id: int, tier: str,
                      nights: int = 8) -> tuple[tuple[date, Decimal], ...]:
    """One hotel's entry price for one tier, night by night, oldest first.

    With a single competitor, its LEVEL is already accounted for by the
    usual gap, so what is left to reason from is its MOVEMENT -- which the
    advisor cannot see in one night's figure. The last ``nights`` nights of
    it, taken from the history rows the caller already loaded.

    A NIGHT IT WAS FULL IS A GAP IN THE LINE, not a repeated price.
    :func:`_night` deliberately keeps a sold-out hotel in the market at the
    last figure it showed, which is right for a median and wrong here: in a
    series of one hotel, that stale number reads as "they held their price"
    on precisely the night they had nothing to sell. Dropped, so a gap in
    the dates is visible as a gap. That the benchmark is full TONIGHT is
    said in the prompt's notes instead, where it is a fact and not a price.
    """
    by_night: dict[date, list] = {}
    for row in rows:
        by_night.setdefault(row[0].check_in, []).append(row)

    out: list[tuple[date, Decimal]] = []
    for night in sorted(by_night):
        _ours, theirs, _sold_out = _night(by_night[night], own_hotel_id)
        entries = [
            c for c in _entry_prices({
                hid: rooms for hid, rooms in theirs.get(tier, {}).items()
                if hid == benchmark_hotel_id
            })
            if not c.sold_out
        ]
        if entries:
            out.append((night, entries[0].price))
    return tuple(out[-nights:])


def propose(rows, *, own_hotel_id: int, rule: Rule,
            room_type_ids: set[int] | None = None,
            usual: dict[int, tuple[Decimal, int]] | None = None,
            night: date | None = None,
            benchmark: Benchmark | None = None,
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

    ``benchmark`` REPLACES all of that for the rooms it can reach. See
    :class:`Benchmark`: the target is that one hotel's entry price for the
    tier, less a fixed number of rupees, and nothing else touches it --
    not the median, not the usual gap, not the weekend or sold-out lift,
    not ``position_pct``, and not ``min_competitors``.

    Each of those is skipped for the same reason rather than as a
    shortcut. The owner's rule names an exact figure, and every one of
    them would move the rate off it: a lift of 10% on a night the other
    hotels are full does not make their gap 100 rupees any more, and
    ``min_competitors`` exists to refuse a market of one hotel that nobody
    chose -- this is a market of one hotel chosen on purpose.

    What still applies is every LIMIT: the step cap, the floor, the
    ceiling, the rounding, and one move per room per night. Those are not
    part of the pricing idea, they are the guard rail around a number
    arriving from outside, and an unbookable benchmark reading is exactly
    what they are for.
    """
    # ONE basis for the whole run. The benchmark decides it, because the
    # benchmark is the only rule here with an opinion: the median compares
    # like with like whichever basis it is on, and this one has a fixed
    # rupee gap that means different things on each.
    with_tax = bool(benchmark and benchmark.with_tax)
    board = benchmark.meal_plan if benchmark else None
    spare_board = benchmark.fallback_board if benchmark else None

    # ONE READING PER BOARD, and each of our rooms takes the first that sells
    # it. The benchmark's side does not move: it is always read on ``board``.
    nights = {board: _night(rows, own_hotel_id, with_tax=with_tax, meal_plan=board)}
    if board and spare_board and spare_board != board:
        nights[spare_board] = _night(rows, own_hotel_id, with_tax=with_tax,
                                     meal_plan=spare_board)

    order: dict[int, str | None] = {}
    for a_board in nights:
        for room_type_id in nights[a_board][0]:
            order.setdefault(room_type_id, a_board)

    proposals = []
    for room_type_id, used_board in order.items():
        if room_type_ids is not None and room_type_id not in room_type_ids:
            continue
        ours, theirs, sold_out = nights[used_board]
        entries = ours[room_type_id]
        room_name = entries[0][1]
        tier = classify(room_name)
        our_price, our_note, our_source = _our_entry(entries)
        competitors = _entry_prices(theirs.get(tier, {}))
        gone = tuple(sorted(sold_out.get(tier, {}).values()))

        base = dict(
            room_type_id=room_type_id, room_name=room_name, tier=tier,
            tier_label=label_for(tier), our_price=our_price, our_note=our_note,
            competitors=competitors, sold_out=gone, board=used_board,
        )
        if tier == OTHER:
            proposals.append(Proposal(**base, market=None, target=None,
                                      held="the room's name states no tier, so it has no market"))
            continue
        if our_price is None:
            proposals.append(Proposal(**base, market=None, target=None,
                                      held="no price of ours on sale tonight to move from"))
            continue

        # PRICED OFF ONE HOTEL. Before the min_competitors gate on purpose:
        # that gate counts a market the owner did not choose, and would
        # refuse this one for having exactly the single hotel it is meant
        # to have.
        if benchmark is not None:
            # PAIRED ROOMS PRICE ALONE. A room nobody paired is left to
            # whoever sets it by hand rather than quietly given a rate from
            # a comparison the owner never chose.
            their_room = None
            if benchmark.room_pairs:
                their_room = benchmark.room_pairs.get(room_type_id)
                if their_room is None:
                    proposals.append(Proposal(
                        **base, market=None, target=None, benchmark=benchmark.hotel,
                        held=f"not paired with a room of {benchmark.hotel}'s",
                    ))
                    continue

            # THEIR SIDE IS ALWAYS READ ON THE PINNED BOARD, even for a room
            # of ours that dropped to the fallback. The owner's rule is "a
            # hundred under Sterling's breakfast rate", and Sterling selling
            # breakfast does not stop because we do not.
            their_rates = nights[board][1]
            entry = (
                _benchmark_room(their_rates, benchmark.hotel_id, our_source, their_room)
                if their_room is not None
                else _benchmark_entry(their_rates.get(tier, {}), benchmark.hotel_id, our_source)
            )
            if entry is None:
                # Deliberately one reason and not two. "No room of that tier"
                # and "no room of that tier on the site we are priced on" are
                # different facts, but the answer to both is the same and the
                # owner reads the sentence, not the branch.
                on_board = f" on {board.lower()}" if board else ""
                what = f'"{their_room}"' if their_room else label_for(tier)
                proposals.append(Proposal(
                    **base, market=None, target=None, benchmark=benchmark.hotel,
                    held=f"{benchmark.hotel} has no {what} on sale tonight{on_board} "
                         f"on the site our own price comes from",
                ))
                continue
            # NOT rounded to ``round_to``. That setting exists because the
            # median rule's aim is approximate anyway and a rate reads better
            # as 5,720 than 5,718. Here the gap IS the instruction -- "a
            # hundred less than Sterling" -- and rounding a 2,936 benchmark
            # to the nearest ten makes it ninety-six less, which is not what
            # anybody set. The limits below still round what they change.
            # ONE OF THEM DID NOT PUBLISH ITS TAX. ``displayed_price`` then
            # falls back to the component it does have and marks it, which
            # is right for a matrix cell and wrong for this: subtracting a
            # hundred from an all-in price to set a pre-tax one is a gap of
            # several hundred wearing the number the owner typed. The marker
            # is the only warning there is, so it stops the proposal.
            if benchmark.with_tax and (entry.note or our_note):
                whose = benchmark.hotel if entry.note else "our own listing"
                proposals.append(Proposal(
                    **base, market=entry.price, target=None, benchmark=benchmark.hotel,
                    benchmark_entry=entry,
                    held=f"{whose} did not publish its tax tonight, so the two prices "
                         f"are not on the same footing to take {benchmark.undercut:,.0f} off",
                ))
                continue

            wanted = entry.price - benchmark.undercut
            target, held, capped = guard(our_price, wanted, rule)
            # NO ONCE-A-NIGHT LOCK ON A FOLLOW, and the difference is not a
            # relaxation of the same idea.
            #
            # Under the median rule the lock earns its place: the target is an
            # AIM at a figure that wobbles, so a room allowed to chase it all
            # evening would walk on noise. A benchmark is an INSTRUCTION about
            # one named hotel -- "a hundred under Sterling" -- and if Sterling
            # moves at four o'clock, a rule that answers "tomorrow" is simply
            # not the rule the owner set. Holding here left us sitting five
            # hundred rupees off their price for the rest of the day.
            #
            # What still stops a runaway is everything that was ever really
            # stopping one: the step cap bounds any single move, the floor and
            # ceiling hold a number outside them, the room's own RMS floor and
            # ceiling are checked after the conversion, and a rate that comes
            # out equal to the cell's is recorded unchanged rather than
            # written. A second run half an hour later therefore does nothing
            # at all unless THEIR price actually moved.
            proposals.append(Proposal(**base, market=entry.price, target=target, held=held,
                                      capped=capped, wanted=wanted, benchmark=benchmark.hotel,
                                      benchmark_entry=entry, exact=True))
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

    # OUR ROOMS THAT HAVE NO RATE ON THE CHOSEN BOARD. They never reach the
    # loop above, because the board filter drops their rows before the rooms
    # are grouped -- so without this they do not appear on the page at all.
    #
    # A room that is simply absent is the one outcome this system refuses
    # everywhere else: the comparison page keeps a category nobody prices so
    # that "nobody sells this" stays visible, and a sold-out room keeps its
    # row. A rate the owner cannot see is a rate the owner cannot fix, and
    # "Booking.com sells this one room-only" is exactly the fact they need in
    # order to go and add a breakfast rate to it.
    if benchmark is not None and board:
        priced = {p.room_type_id for p in proposals}
        theirs_named = benchmark.room_pairs.get if benchmark.room_pairs else lambda _: None
        for series, hotel, room_name in rows:
            room_type_id = series.room_type_id
            if hotel.id != own_hotel_id or room_type_id in priced:
                continue
            if room_type_ids is not None and room_type_id not in room_type_ids:
                continue
            priced.add(room_type_id)
            tier = classify(room_name)
            paired = theirs_named(room_type_id)
            # ITS CHEAPEST RATE ON WHATEVER BOARD IT DOES SELL. The rule will
            # not price this room -- there is nothing on the chosen board to
            # compare -- but the owner still wants to type a figure at it,
            # and a typed guest price is only convertible to an RMS rate
            # while there is a price of ours to take the ratio from. Carried
            # with the board it belongs to, so the RMS row written is the one
            # that actually sells it.
            fallback = _fallback_rate(rows, own_hotel_id, room_type_id, with_tax)
            proposals.append(Proposal(
                room_type_id=room_type_id, room_name=room_name, tier=tier,
                tier_label=label_for(tier),
                our_price=fallback[0], our_note=fallback[1], board=fallback[2],
                competitors=(), sold_out=(), market=None, target=None,
                benchmark=benchmark.hotel if paired else None,
                held=f"we sell this room on neither {board.lower()} nor "
                     f"{(spare_board or '').lower() or 'any other board'} tonight"
                     + (f", so there is nothing to compare with {benchmark.hotel}'s "
                        f'"{paired}"' if paired else "")
                     + (f" (showing our {fallback[2].lower()} rate)" if fallback[2] else ""),
            ))

    # THE PAIRED ROOMS FIRST. They are the ones the rule has something to
    # say about, and the owner opens this page to act on them; the rest are
    # there to be typed at. Within each group the old order is kept.
    pairs = benchmark.room_pairs if benchmark else {}
    proposals.sort(key=lambda p: (0 if p.room_type_id in pairs else 1,
                                  p.tier_label, p.room_name))
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
    return replace(proposal, target=price, held=None, capped=f"your price ({was})", exact=True)


def to_rms(target_guest: Decimal, our_guest: Decimal, current_rms: Decimal, *,
           round_to: int, exact: bool = False) -> Decimal:
    """The RMS rate that would make the channel show ``target_guest``.

    By the ratio the channel is applying today: RMS shows 7,500 where the
    site shows 5,610, so a target of 6,000 on the site is 6,000 × 7,500/5,610
    in RMS. Read both on the same morning, so the deal in force is the deal
    in the ratio.

    ``round_to`` IS COSMETIC AND THIS IS THE ONE PLACE IT CANNOT AFFORD TO BE.
    The setting exists because the median rule's aim is approximate anyway and
    a rate reads better as 5,720 than 5,718. But the ratio here is about 0.56,
    so ten rupees of tidiness on the RMS rate comes back out as six on the
    line the guest reads -- and where the target was an instruction rather
    than an aim, those six rupees are the instruction being disobeyed. A gap
    the owner set at a hundred under Sterling was applied as ninety-eight:
    5,216 became RMS 9,290 and came back out at 5,218. The target had already
    refused to round itself for exactly this reason (see :func:`propose`); it
    was rounded here instead, one step further down the pipe.

    So ``exact`` -- a benchmark follow, or a price the owner typed -- takes
    the whole rupee BELOW the ratio's answer. Never above: the gap the owner
    set is a promise about how far under the other hotel we sit, and landing
    a rupee further under keeps it where landing a rupee over breaks it.
    """
    if not our_guest:
        raise ValueError("our guest price is zero; no ratio to scale by")
    wanted = target_guest * current_rms / our_guest
    if exact:
        return wanted.quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return _round_to(wanted, round_to)


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


def settled(current: Decimal | None, proposed: Decimal | None, *, round_to: int) -> bool:
    """True when the proposal would not really move the price.

    ASK IT OF THE PRICES, NOT OF A REMEMBERED RATE. The page used to test
    the RMS cell against the proposed rate, and the cell is a MEMORY -- the
    last number this app wrote or read, not what the grid holds now. When
    the owner put tonight's rates back by hand, Booking.com returned to the
    price our last write had replaced, the remembered rate still said 5,082,
    and the row announced "no change needed, already 100 under Sterling"
    while sitting 2,950 ABOVE them.

    Our own guest price and theirs are both freshly fetched, and the gap
    between them is the whole of what the rule is about, so that is the
    comparison: are we already where the rule wants us? Nothing remembered
    takes part in it, so nothing stale can make it lie.

    (The RUN still compares the live cell against the rate it is about to
    write -- that reading is seconds old and is the right question there.)

    A FOLLOW THAT HAS CAUGHT UP HAS NOTHING TO SAY, and the page should say
    nothing rather than offer a price. Once we sit a hundred under them, the
    rule recomputes the same number every half hour: the box kept showing a
    "proposed price" identical to today's, at +0.0%, with a green "will
    apply" beside it -- three pieces of furniture for a decision nobody has
    to make. Worse, it invites the owner to press Apply and sign into RMS to
    write the number already there.

    THE THRESHOLD IS ``round_to``, not zero, and not a constant picked here.
    The trip from a guest price to an RMS rate and back rounds twice, so a
    settled room lands a rupee or two off its own last answer -- 5,844
    becomes 5,845 -- and an exact test would call that a move and write it.
    ``round_to`` is the owner's own statement of the smallest step a rate
    should take; anything under it is arithmetic noise, not a decision. An
    owner who wants finer control lowers it and gets it.
    """
    if current is None or proposed is None:
        return False
    return abs(int(current) - int(proposed)) < max(int(round_to), 1)


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
