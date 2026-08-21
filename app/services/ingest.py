"""From adapter output to stored history and confirmed changes.

This is the join between the pure logic (``offer_key``, ``room_matching``,
``comparison``) and the database. Those modules stay pure and heavily tested;
this one is the thin, ordered layer that persists their decisions.

The order of operations is the whole design:

1. **Resolve the room name to a room type.** No confident match means an
   ``UnmatchedOffer`` row and nothing else — never a guess, because a wrong
   mapping corrupts a price series silently and indefinitely, while a gap is
   visible and gets fixed.
2. **Compute the offer key**, which decides what this price is comparable to.
3. **Write the observation, always.** Even when nothing changed, even when the
   room is sold out. This table is the history and it is append-only.
4. **Compare against the series row** and let ``services.comparison`` decide.
5. **Write a change row only for a confirmed, notifiable outcome.**

Step 3 happening before step 4 matters: if the process dies between them, we
have lost a comparison but not a data point, and the next run recovers. The
reverse order would lose the data point permanently.

DISAPPEARANCE
=============
A room vanishing from the page is a real event and is handled explicitly at
the end: any series for this hotel/source/stay that we did NOT see in a
successful fetch is treated as an unavailability observation. Without this, a
sold-out room would simply freeze at its last price forever and the dashboard
would quietly lie.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.adapters.base import FetchResult, NormalizedOffer
from app.core.logging import get_logger
from app.core.redaction import scrub
from app.db.models import (
    MatchMethod,
    PriceBasis,
    PriceChange,
    PriceObservation,
    PriceSeries,
    RoomType,
    RoomTypeAlias,
    UnmatchedOffer,
)
from app.services import room_matching
from app.services.comparison import (
    CarryOver,
    Observation,
    Outcome,
    SeriesState,
    Thresholds,
    compare,
    compare_across_stay_dates,
)
from app.services.dates import StayWindow
from app.services.offer_key import compute_offer_key

log = get_logger("ingest")


@dataclass(slots=True)
class IngestSummary:
    """What one fetch produced. Written onto the ``check_runs`` row."""

    offers_seen: int = 0
    offers_matched: int = 0
    offers_unmatched: int = 0
    change_ids: list[int] = field(default_factory=list)
    outcomes: Counter = field(default_factory=Counter)
    #: Offers discarded because an earlier offer in the SAME fetch already
    #: claimed their identity. Counted rather than merely logged: this is the
    #: shape a broken room_name selector takes by the time it reaches the
    #: database, and it is otherwise invisible -- the fetch succeeds, the run
    #: reports six offers found and none unmatched, and five rooms quietly
    #: cease to exist. See :func:`ingest_fetch_result`.
    offers_collapsed: int = 0
    #: The room names involved, for the message a person eventually reads.
    collapsed_names: list[str] = field(default_factory=list)

    @property
    def changes_detected(self) -> int:
        return len(self.change_ids)


@dataclass(frozen=True, slots=True)
class IngestContext:
    """Everything the pipeline needs that is not in the fetch result itself."""

    hotel_id: int
    source_id: int
    hotel_source_id: int
    stay: StayWindow
    adults: int
    children: int
    currency: str
    price_basis: PriceBasis
    thresholds: Thresholds
    checked_at: datetime
    check_run_id: str | None = None
    meal_plan_filter: str | None = None


def ingest_fetch_result(
    session: Session, result: FetchResult, ctx: IngestContext
) -> IngestSummary:
    """Persist one adapter run. Returns what happened, for the check-run row.

    The caller owns the transaction: everything here is one atomic unit, so a
    failure halfway leaves no half-updated series behind.
    """
    summary = IngestSummary(offers_seen=len(result.offers))

    aliases = _load_aliases(session, ctx)
    candidates = _load_room_types(session, ctx.hotel_id)
    if not candidates and result.offers:
        # A brand-new hotel has no rooms defined, so every offer would land in
        # the unmatched queue and an operator would retype names the site has
        # already told us. Seeding from the site's own names is not guessing:
        # with zero existing rooms there is nothing to mis-map against, which
        # is exactly the condition that makes matching risky elsewhere.
        candidates = _seed_room_types(session, result.offers, ctx)
    seen_keys: set[str] = set()

    for offer in result.offers:
        if _filtered_out(offer, ctx):
            continue

        room_type_id = _resolve_room(session, offer, ctx, aliases, candidates)
        if room_type_id is None:
            summary.offers_unmatched += 1
            continue
        summary.offers_matched += 1

        offer_key = compute_offer_key(
            hotel_id=ctx.hotel_id,
            source_id=ctx.source_id,
            room_type_id=room_type_id,
            check_in=ctx.stay.check_in,
            check_out=ctx.stay.check_out,
            adults=ctx.adults,
            children=ctx.children,
            meal_plan=offer.meal_plan,
            refundable=offer.refundable,
            currency=offer.currency or ctx.currency,
        )
        if offer_key in seen_keys:
            # Two offers in one fetch resolved to the same identity. Legitimate
            # when a site lists one room under two rate plans that differ only
            # in something the key does not carry; a bug when two DIFFERENT
            # rooms matched the same room type.
            #
            # Either way the second must not be inserted: the offer key is the
            # primary key of price_series, so writing both aborts the whole
            # transaction and the entire fetch is lost — including the rooms
            # that were perfectly fine.
            log.warning(
                "duplicate_offer_key_in_fetch",
                hotel_id=ctx.hotel_id,
                offer_key=offer_key[:12],
                raw_name=offer.raw_room_name[:80],
                room_type_id=room_type_id,
                hint="two room names mapped to one room type; check the alias table",
            )
            # Counted, not just logged. A log line is read by nobody after the
            # fact, and this is the exact footprint of a room_name selector
            # that has landed on something every card shares: the fetch
            # succeeds, the check run records six offers found and zero
            # unmatched, and five of the hotel's rooms are simply absent from
            # every screen. The caller turns a sustained count into a row on
            # Attention, because a person is the only thing that can fix it.
            summary.offers_collapsed += 1
            if offer.raw_room_name not in summary.collapsed_names:
                summary.collapsed_names.append(offer.raw_room_name[:80])
            continue
        seen_keys.add(offer_key)

        observation_id = _write_observation(session, offer, offer_key, ctx)
        outcome = _apply_comparison(
            session,
            offer_key=offer_key,
            room_type_id=room_type_id,
            observation=Observation(
                price=offer.price_on(ctx.price_basis.value),
                is_available=offer.is_available,
            ),
            offer=offer,
            observation_id=observation_id,
            ctx=ctx,
            summary=summary,
        )
        summary.outcomes[outcome] += 1

    _handle_disappearances(session, seen_keys, result, ctx, summary)
    return summary


# -- room resolution -------------------------------------------------
def _load_aliases(session: Session, ctx: IngestContext) -> dict[str, int]:
    rows = session.execute(
        select(RoomTypeAlias.normalized_name, RoomTypeAlias.room_type_id).where(
            RoomTypeAlias.source_id == ctx.source_id,
            RoomTypeAlias.hotel_id == ctx.hotel_id,
        )
    ).all()
    return {name: room_type_id for name, room_type_id in rows}


def _load_room_types(session: Session, hotel_id: int) -> list[tuple[int, str]]:
    rows = session.execute(
        select(RoomType.id, RoomType.canonical_name).where(
            RoomType.hotel_id == hotel_id, RoomType.is_active.is_(True)
        )
    ).all()
    return [(room_id, canonical) for room_id, canonical in rows]


def _seed_room_types(
    session: Session, offers: list[NormalizedOffer], ctx: IngestContext
) -> list[tuple[int, str]]:
    """Create a hotel's room types from the first successful fetch.

    Only ever runs when the hotel has none at all. The names come from the
    site, which is the authority on what its rooms are called — an operator
    typing them by hand is copying from the same source, with typos.

    Two names that normalise identically collapse into one room type, which is
    correct: they are the same room described twice.
    """
    created: list[tuple[int, str]] = []
    seen: set[str] = set()

    for order, offer in enumerate(offers):
        canonical = room_matching.normalize_room_name(offer.raw_room_name)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)

        room = RoomType(
            hotel_id=ctx.hotel_id,
            name=offer.raw_room_name.strip()[:200],
            canonical_name=canonical[:200],
            sort_order=order,
        )
        session.add(room)
        session.flush()  # need the id to match the rest of this same fetch
        created.append((room.id, canonical))

    if created:
        log.info(
            "room_types_seeded",
            hotel_id=ctx.hotel_id,
            count=len(created),
            names=[o.raw_room_name[:40] for o in offers][:8],
        )
    return created


def _resolve_room(
    session: Session,
    offer: NormalizedOffer,
    ctx: IngestContext,
    aliases: dict[str, int],
    candidates: list[tuple[int, str]],
) -> int | None:
    """Map a raw room name to a room type, or record it as unmatched.

    A fuzzy match above the automatic threshold writes an alias row, so the
    same rename costs one fuzzy comparison once rather than on every run — and
    so the mapping is visible and correctable in the dashboard instead of
    living only in the matcher's head.
    """
    match = room_matching.resolve(
        offer.raw_room_name, aliases=aliases, candidates=candidates
    )

    if match.room_type_id is not None:
        if not match.is_exact:
            _record_alias(session, match, offer, ctx)
            aliases[match.normalized] = match.room_type_id
        _clear_unmatched(session, match, offer, ctx)
        return match.room_type_id

    _record_unmatched(session, match, offer, ctx)
    log.info(
        "offer_unmatched",
        hotel_id=ctx.hotel_id,
        raw_name=offer.raw_room_name[:80],
        best_score=match.score,
    )
    return None


def _record_alias(session: Session, match, offer: NormalizedOffer, ctx: IngestContext) -> None:
    """Persist an automatically-accepted fuzzy match.

    ``ON CONFLICT DO NOTHING``: two workers can resolve the same new room name
    in the same cycle, and a race here must not fail an otherwise good fetch.
    """
    session.execute(
        pg_insert(RoomTypeAlias)
        .values(
            room_type_id=match.room_type_id,
            hotel_id=ctx.hotel_id,
            source_id=ctx.source_id,
            raw_name=offer.raw_room_name[:300],
            normalized_name=match.normalized[:300],
            match_method=MatchMethod.FUZZY,
            confidence=round(match.score / 100, 3) if match.score is not None else None,
        )
        .on_conflict_do_nothing(index_elements=["source_id", "hotel_id", "normalized_name"])
    )


def _record_unmatched(session: Session, match, offer: NormalizedOffer, ctx: IngestContext) -> None:
    """Upsert the dashboard's "needs mapping" queue.

    Counting occurrences rather than inserting a row per sighting keeps the
    queue at one line per unknown room, which is what makes it something an
    operator will actually clear.
    """
    normalized = (match.normalized or offer.raw_room_name.lower())[:300]
    statement = (
        pg_insert(UnmatchedOffer)
        .values(
            hotel_source_id=ctx.hotel_source_id,
            raw_room_name=offer.raw_room_name[:300],
            normalized_name=normalized,
            sample_payload=scrub(offer.raw_payload) if offer.raw_payload else None,
            suggested_room_type_id=match.suggestion.room_type_id if match.suggestion else None,
            suggested_confidence=(
                round(match.suggestion.score / 100, 3) if match.suggestion else None
            ),
            first_seen_at=ctx.checked_at,
            last_seen_at=ctx.checked_at,
            occurrence_count=1,
        )
        .on_conflict_do_update(
            index_elements=["hotel_source_id", "normalized_name"],
            set_={
                "last_seen_at": ctx.checked_at,
                "occurrence_count": UnmatchedOffer.__table__.c.occurrence_count + 1,
            },
        )
    )
    session.execute(statement)


def _clear_unmatched(
    session: Session, match, offer: NormalizedOffer, ctx: IngestContext
) -> None:
    """Close a queued "needs mapping" row once its name maps on its own.

    Nothing used to do this, and the queue only ever grew. An offer is queued
    when no room type matches it; if a matching room type appears later — an
    operator creating one, or the pipeline seeding a hotel's rooms after a
    repair — the offer starts resolving perfectly and its prices are recorded
    correctly, while the row asking a human to map it stays open forever.

    That is worse than untidy. The queue is meant to be a short list of things
    only a person can decide, and it stops being read once it fills with work
    that no longer needs doing. An operator clearing one of these would also be
    creating an alias for a mapping that already resolves without it.

    The evidence is exact rather than inferred: THIS offer, the one that was
    queued under this name, has just resolved. Rows for names that are still
    genuinely unmappable are untouched.
    """
    normalized = (match.normalized or offer.raw_room_name.lower())[:300]
    row = session.execute(
        select(UnmatchedOffer).where(
            UnmatchedOffer.hotel_source_id == ctx.hotel_source_id,
            UnmatchedOffer.normalized_name == normalized,
            UnmatchedOffer.resolved_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return

    row.resolved_at = ctx.checked_at
    row.suggested_room_type_id = match.room_type_id
    log.info(
        "unmatched_offer_self_resolved",
        hotel_id=ctx.hotel_id,
        raw_name=offer.raw_room_name[:80],
        room_type_id=match.room_type_id,
    )


def _filtered_out(offer: NormalizedOffer, ctx: IngestContext) -> bool:
    """Honour a target's meal-plan filter.

    Filtering here rather than in the adapter keeps the adapter's job to "read
    the page" and keeps this a business rule that can change without touching
    a site integration.
    """
    if not ctx.meal_plan_filter:
        return False
    if offer.meal_plan is None:
        return True
    return ctx.meal_plan_filter.strip().lower() not in offer.meal_plan.strip().lower()


# -- persistence -----------------------------------------------------
def _write_observation(
    session: Session, offer: NormalizedOffer, offer_key: str, ctx: IngestContext
) -> int | None:
    """Append the history row.

    ``ON CONFLICT DO NOTHING`` on (offer_key, checked_at) makes a Celery retry
    idempotent: re-running a task that already wrote its observations produces
    no duplicates and no error.
    """
    statement = (
        pg_insert(PriceObservation)
        .values(
            checked_at=ctx.checked_at,
            offer_key=offer_key,
            price_exclusive=offer.price_exclusive,
            taxes_fees=offer.taxes_fees,
            price_inclusive=offer.price_inclusive,
            currency=(offer.currency or ctx.currency)[:3].upper(),
            is_available=offer.is_available,
            rooms_left=offer.rooms_left,
            raw_room_name=offer.raw_room_name[:300],
            raw_payload=scrub(offer.raw_payload) if offer.raw_payload else None,
            check_run_id=ctx.check_run_id,
        )
        .on_conflict_do_nothing(constraint="uq_observation_offer_time")
        .returning(PriceObservation.id)
    )
    return session.execute(statement).scalar_one_or_none()


def _apply_comparison(
    session: Session,
    *,
    offer_key: str,
    room_type_id: int,
    observation: Observation,
    offer: NormalizedOffer,
    observation_id: int | None,
    ctx: IngestContext,
    summary: IngestSummary,
) -> Outcome:
    """Compare, persist the new series state, and record a change if confirmed."""
    series = session.execute(
        select(PriceSeries).where(PriceSeries.offer_key == offer_key).with_for_update()
    ).scalar_one_or_none()

    # A series recorded on the other basis cannot be compared against this
    # observation: ₹1,048.95 inclusive and ₹999 exclusive are the SAME price,
    # and subtracting one from the other invents a 4.8% drop, records a
    # price_changes row for it and buzzes somebody's phone. Passing no state
    # re-baselines the series instead -- the new number is stored, the pending
    # counter is cleared and nothing is reported. One quiet check per series,
    # once, when the basis is changed.
    rebased = series is not None and series.last_price_basis != ctx.price_basis
    if rebased:
        log.info(
            "price_series_rebased",
            offer_key=offer_key[:12],
            hotel_id=ctx.hotel_id,
            from_basis=series.last_price_basis.value,
            to_basis=ctx.price_basis.value,
            old_price=str(series.last_price),
            new_price=str(observation.price),
        )

    state = (
        SeriesState(
            last_price=series.last_price,
            is_available=series.is_available,
            pending_price=series.pending_price,
            pending_count=series.pending_count,
        )
        if series is not None and not rebased
        else None
    )

    decision = compare(state, observation, ctx.thresholds)
    new_state = decision.new_state

    if series is None:
        series = PriceSeries(
            offer_key=offer_key,
            hotel_id=ctx.hotel_id,
            room_type_id=room_type_id,
            source_id=ctx.source_id,
            check_in=ctx.stay.check_in,
            check_out=ctx.stay.check_out,
            adults=ctx.adults,
            children=ctx.children,
            meal_plan=offer.meal_plan,
            refundable=offer.refundable,
            currency=(offer.currency or ctx.currency)[:3].upper(),
            last_price_basis=ctx.price_basis,
            first_seen_at=ctx.checked_at,
            last_checked_at=ctx.checked_at,
        )
        session.add(series)

    is_first_sight = decision.outcome is Outcome.FIRST_SIGHT

    # When this new price was FIRST seen, captured before the pending state is
    # overwritten. A change is written on its second sighting, so `checked_at`
    # is when the debounce finished; this is when the hotel's new price
    # actually appeared, which is the time a person means by "when did it
    # change".
    first_seen_at = series.pending_since or ctx.checked_at

    # Unconditionally, whatever the comparison decided. This is the number the
    # dashboard and the API show, and it must equal what the hotel's own
    # booking page is displaying right now -- including for a move too small to
    # alert on, which is precisely the case that used to never reach a screen.
    #
    # Kept distinct from `last_price` on purpose: see PriceSeries for why the
    # confirmed baseline has to stay put while this one tracks every check.
    # A sold-out check carries no price, and blanking the last known rate on
    # one would lose it for as long as the room stays unavailable, so the
    # previous value is left standing and `is_available` carries the state.
    if observation.price is not None:
        series.current_price = observation.price

    series.last_price = new_state.last_price
    series.last_price_basis = ctx.price_basis
    series.is_available = new_state.is_available

    # `pending_since` only moves when the pending price itself changes.
    #
    # Stamping it on every check quietly made it "pending as of the last
    # check". With confirm_checks=2 that is still the first sighting and the
    # bug is invisible; at 3 or more it slides forward one check at a time and
    # the recorded first-seen time is wrong by exactly the amount that makes
    # it look plausible.
    if new_state.pending_price is None:
        series.pending_since = None
    elif series.pending_price != new_state.pending_price or series.pending_since is None:
        series.pending_since = ctx.checked_at

    series.pending_price = new_state.pending_price
    series.pending_count = new_state.pending_count
    series.last_checked_at = ctx.checked_at

    if is_first_sight:
        # A new stay date has no history of its own, so `compare` correctly
        # said nothing. But under a rolling target this IS tonight's rate, and
        # last night's rate for the same room is the baseline a human would
        # use. That comparison is the whole point of the monitor and lives
        # nowhere else -- without it a lead_time_days=0 target can never
        # produce a single alert, because every day is a first sighting.
        _carry_over_change(
            session,
            offer_key=offer_key,
            room_type_id=room_type_id,
            observation=observation,
            offer=offer,
            observation_id=observation_id,
            currency=series.currency,
            ctx=ctx,
            summary=summary,
        )

    if decision.should_record_change:
        series.last_changed_at = ctx.checked_at
        change = PriceChange(
            offer_key=offer_key,
            hotel_id=ctx.hotel_id,
            changed_at=ctx.checked_at,
            first_seen_at=first_seen_at,
            old_price=decision.old_price,
            new_price=decision.new_price,
            delta=decision.delta,
            delta_pct=decision.delta_pct,
            currency=series.currency,
            direction=decision.direction,
            observation_id_new=observation_id,
            notified=False,
        )
        session.add(change)
        session.flush()  # need the id to hand to the notify task
        summary.change_ids.append(change.id)
        log.info(
            "price_change_confirmed",
            hotel_id=ctx.hotel_id,
            offer_key=offer_key[:12],
            direction=str(decision.direction),
            old_price=str(decision.old_price),
            new_price=str(decision.new_price),
        )

    return decision.outcome


def _previous_stay_series(
    session: Session,
    *,
    room_type_id: int,
    offer: NormalizedOffer,
    ctx: IngestContext,
) -> PriceSeries | None:
    """The same room, same booking conditions, most recent EARLIER stay date.

    Everything the offer key holds constant is held constant here too, except
    the dates themselves — plus the stay LENGTH, because a one-night rate and a
    two-night rate are not comparable even for the same room.

    ``check_in < ctx.stay.check_in`` rather than ``= yesterday`` so a monitor
    that was switched off over a weekend still finds its last real baseline;
    how far back is too far is the caller's judgement, enforced in
    :func:`compare_across_stay_dates`.
    """
    nights = (ctx.stay.check_out - ctx.stay.check_in).days
    meal_plan = offer.meal_plan
    refundable = offer.refundable
    currency = (offer.currency or ctx.currency)[:3].upper()

    conditions = [
        PriceSeries.hotel_id == ctx.hotel_id,
        PriceSeries.source_id == ctx.source_id,
        PriceSeries.room_type_id == room_type_id,
        PriceSeries.adults == ctx.adults,
        PriceSeries.children == ctx.children,
        PriceSeries.currency == currency,
        PriceSeries.check_in < ctx.stay.check_in,
        (PriceSeries.check_out - PriceSeries.check_in) == nights,
    ]
    # NULL is a real value for these two -- "meal plan unknown" is a different
    # offer from "room only" -- so they need IS NULL, not = NULL, which never
    # matches anything and would silently disable the whole comparison.
    conditions.append(
        PriceSeries.meal_plan.is_(None) if meal_plan is None
        else PriceSeries.meal_plan == meal_plan
    )
    conditions.append(
        PriceSeries.refundable.is_(None) if refundable is None
        else PriceSeries.refundable == refundable
    )

    return session.execute(
        select(PriceSeries)
        .where(*conditions)
        .order_by(PriceSeries.check_in.desc())
        .limit(1)
    ).scalar_one_or_none()


def _carry_over_change(
    session: Session,
    *,
    offer_key: str,
    room_type_id: int,
    observation: Observation,
    offer: NormalizedOffer,
    observation_id: int | None,
    currency: str,
    ctx: IngestContext,
    summary: IngestSummary,
) -> None:
    """Compare a brand-new series against the last stay date we priced.

    Called ONLY on first sighting, which is what makes it safe to run without
    a debounce: a given stay date is first seen exactly once, so this can emit
    at most one row per series no matter how often the target is checked.
    """
    previous = _previous_stay_series(
        session, room_type_id=room_type_id, offer=offer, ctx=ctx
    )
    if previous is None:
        return

    # The main path rebases a series when the configured basis changes, but
    # this one reads a DIFFERENT series -- an earlier stay date, which may hold
    # a price recorded on the old basis and, its date having passed, may never
    # be checked again to rebase itself. Comparing across the two bases would
    # publish the tax component as a price move, with no debounce to catch it.
    if previous.last_price_basis != ctx.price_basis:
        log.info(
            "carry_over_skipped_basis_mismatch",
            hotel_id=ctx.hotel_id,
            offer_key=offer_key[:12],
            previous_offer_key=previous.offer_key[:12],
            previous_basis=previous.last_price_basis.value,
            basis=ctx.price_basis.value,
        )
        return

    change = compare_across_stay_dates(
        CarryOver(
            last_price=previous.last_price,
            is_available=previous.is_available,
            check_in=previous.check_in,
            offer_key=previous.offer_key,
        ),
        observation,
        ctx.thresholds,
        this_check_in=ctx.stay.check_in,
    )
    if change is None:
        return

    # First sighting happens once per series, so this should already be
    # unique -- unless the series row was deleted and rebuilt, which recreates
    # the "first" sighting of a stay date that has already been reported. The
    # cost of being wrong here is telling someone twice about one price move,
    # which is the exact failure the whole comparison layer exists to avoid,
    # so the uniqueness is checked rather than assumed.
    already = session.execute(
        select(PriceChange.id).where(
            PriceChange.offer_key == offer_key,
            PriceChange.previous_offer_key == change.previous_offer_key,
        )
    ).first()
    if already is not None:
        log.info(
            "carry_over_change_already_recorded",
            hotel_id=ctx.hotel_id,
            offer_key=offer_key[:12],
            previous_offer_key=change.previous_offer_key[:12],
        )
        return

    row = PriceChange(
        offer_key=offer_key,
        hotel_id=ctx.hotel_id,
        changed_at=ctx.checked_at,
        # This comparison runs at first sighting, so the price was seen and
        # reported in the same breath.
        first_seen_at=ctx.checked_at,
        old_price=change.old_price,
        new_price=change.new_price,
        delta=change.delta,
        delta_pct=change.delta_pct,
        currency=currency,
        direction=change.direction,
        observation_id_new=observation_id,
        previous_offer_key=change.previous_offer_key,
        notified=False,
    )
    session.add(row)
    session.flush()  # the notify task is handed the id
    summary.change_ids.append(row.id)
    log.info(
        "carry_over_change_confirmed",
        hotel_id=ctx.hotel_id,
        offer_key=offer_key[:12],
        previous_offer_key=change.previous_offer_key[:12],
        previous_check_in=str(change.previous_check_in),
        check_in=str(ctx.stay.check_in),
        direction=str(change.direction),
        old_price=str(change.old_price),
        new_price=str(change.new_price),
        delta_pct=str(change.delta_pct),
    )


# -- disappearance ---------------------------------------------------
def _handle_disappearances(
    session: Session,
    seen_keys: set[str],
    result: FetchResult,
    ctx: IngestContext,
    summary: IngestSummary,
) -> None:
    """Mark series we used to see, and no longer do, as unavailable.

    Only runs when the fetch actually succeeded at reading the page — either it
    returned offers, or it positively detected a sold-out state. An empty
    result from a broken adapter never reaches here, because the adapter raises
    ``SchemaDriftError`` instead; that distinction is what stops a site
    redesign from being reported as "every room sold out".
    """
    if not result.offers and not result.sold_out_detected:
        return

    stale = session.execute(
        select(PriceSeries).where(
            PriceSeries.hotel_id == ctx.hotel_id,
            PriceSeries.source_id == ctx.source_id,
            PriceSeries.check_in == ctx.stay.check_in,
            PriceSeries.check_out == ctx.stay.check_out,
            PriceSeries.adults == ctx.adults,
            PriceSeries.children == ctx.children,
            PriceSeries.is_available.is_(True),
        )
    ).scalars().all()

    for series in stale:
        if series.offer_key in seen_keys:
            continue

        _write_observation(
            session,
            NormalizedOffer(
                raw_room_name="(not listed)",
                is_available=False,
                currency=series.currency,
            ),
            series.offer_key,
            ctx,
        )

        decision = compare(
            SeriesState(
                last_price=series.last_price,
                is_available=True,
                pending_price=series.pending_price,
                pending_count=series.pending_count,
            ),
            Observation(price=None, is_available=False),
            ctx.thresholds,
        )

        series.is_available = decision.new_state.is_available
        series.pending_price = None
        series.pending_count = 0
        series.pending_since = None
        series.last_checked_at = ctx.checked_at
        series.last_changed_at = ctx.checked_at

        change = PriceChange(
            offer_key=series.offer_key,
            hotel_id=ctx.hotel_id,
            changed_at=ctx.checked_at,
            # A room that has gone is reported on the check that finds it
            # missing, with no debounce, so there is no earlier sighting to
            # point at.
            first_seen_at=ctx.checked_at,
            old_price=series.last_price,
            new_price=None,
            delta=None,
            delta_pct=None,
            currency=series.currency,
            direction=decision.direction,
            notified=False,
        )
        session.add(change)
        session.flush()
        summary.change_ids.append(change.id)
        summary.outcomes[Outcome.BECAME_UNAVAILABLE] += 1
        log.info(
            "room_no_longer_listed",
            hotel_id=ctx.hotel_id,
            offer_key=series.offer_key[:12],
            last_price=str(series.last_price),
        )


def price_from(offer: NormalizedOffer, basis: PriceBasis) -> Decimal | None:
    """Public helper: the number this offer would be compared on."""
    return offer.price_on(basis.value)
