"""The ingest pipeline, against a real PostgreSQL.

This is the code that turns adapter output into stored history and confirmed
changes. The pure decisions inside it are already covered by the comparison and
room-matching unit tests; what needs a real database is everything those tests
cannot see: the ``ON CONFLICT`` upserts, the partitioned insert, ``SELECT FOR
UPDATE`` on the series row, and the disappearance sweep.

Skipped unless ``TEST_DATABASE_URL`` is set — see ``conftest.py``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.adapters.base import FetchResult, NormalizedOffer
from app.db.models import (
    ChangeDirection,
    PriceBasis,
    PriceChange,
    PriceObservation,
    PriceSeries,
    RoomType,
    RoomTypeAlias,
    UnmatchedOffer,
)
from app.services.comparison import Outcome, Thresholds
from app.services.dates import StayWindow
from app.services.ingest import IngestContext, ingest_fetch_result

pytestmark = pytest.mark.integration

STAY = StayWindow(datetime(2026, 12, 20).date(), datetime(2026, 12, 21).date())


def _context(
    fixture, *, checked_at=None, confirm_checks=2, price_basis=PriceBasis.INCLUSIVE
) -> IngestContext:
    return IngestContext(
        hotel_id=fixture["hotel"].id,
        source_id=fixture["source"].id,
        hotel_source_id=fixture["hotel_source"].id,
        stay=STAY,
        adults=2,
        children=0,
        currency="INR",
        price_basis=price_basis,
        thresholds=Thresholds(
            min_delta_abs=Decimal("50"),
            min_delta_pct=Decimal("2.0"),
            confirm_checks=confirm_checks,
        ),
        checked_at=checked_at or datetime.now(UTC),
        check_run_id=None,
    )


def _offer(name="Deluxe Room", price="3000", available=True, **kw) -> NormalizedOffer:
    return NormalizedOffer(
        raw_room_name=name,
        price_inclusive=Decimal(price) if price is not None else None,
        currency="INR",
        is_available=available,
        **kw,
    )


def _result(*offers, sold_out=False) -> FetchResult:
    return FetchResult(offers=list(offers), sold_out_detected=sold_out)


class TestFirstSighting:
    def test_records_history_and_tells_nobody(self, session, hotel_fixture):
        """Adding a hotel must not immediately spam its recipient."""
        summary = ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture))
        session.flush()

        assert summary.offers_matched == 1
        assert summary.changes_detected == 0
        assert summary.outcomes[Outcome.FIRST_SIGHT] == 1

        series = session.scalars(select(PriceSeries)).all()
        assert len(series) == 1
        assert series[0].last_price == Decimal("3000.00")

        # The observation is written regardless: it is the history.
        assert session.scalar(select(func.count(PriceObservation.id))) == 1

    def test_offer_key_is_the_primary_key(self, session, hotel_fixture):
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture))
        session.flush()
        series = session.scalars(select(PriceSeries)).one()
        assert len(series.offer_key) == 64


class TestDebounce:
    def test_a_move_must_persist_before_it_counts(self, session, hotel_fixture):
        """The rule that keeps the alerts trustworthy.

        Dynamic pricing and A/B tests produce one-off blips. Without the
        confirmation requirement every blip becomes an alert, the alerts become
        noise, and the noise gets them ignored — which is the real failure.
        """
        base = datetime.now(UTC)
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture, checked_at=base))
        session.flush()

        # First sighting of the new price: significant, not yet confirmed.
        summary = ingest_fetch_result(
            session,
            _result(_offer(price="2700")),
            _context(hotel_fixture, checked_at=base + timedelta(minutes=30)),
        )
        session.flush()
        assert summary.changes_detected == 0
        assert summary.outcomes[Outcome.PENDING_CONFIRMATION] == 1

        series = session.scalars(select(PriceSeries)).one()
        assert series.last_price == Decimal("3000.00")   # baseline is untouched
        assert series.pending_price == Decimal("2700.00")

        # Second consecutive sighting: confirmed.
        summary = ingest_fetch_result(
            session,
            _result(_offer(price="2700")),
            _context(hotel_fixture, checked_at=base + timedelta(minutes=60)),
        )
        session.flush()
        assert summary.changes_detected == 1

        change = session.scalars(select(PriceChange)).one()
        assert change.old_price == Decimal("3000.00")
        assert change.new_price == Decimal("2700.00")
        assert change.direction.value == "decrease"
        assert change.delta_pct == Decimal("-10.00")

    def test_a_blip_that_reverts_never_alerts(self, session, hotel_fixture):
        base = datetime.now(UTC)
        for minutes, price in ((0, "3000"), (30, "2700"), (60, "3000")):
            ingest_fetch_result(
                session,
                _result(_offer(price=price)),
                _context(hotel_fixture, checked_at=base + timedelta(minutes=minutes)),
            )
            session.flush()

        assert session.scalar(select(func.count(PriceChange.id))) == 0
        series = session.scalars(select(PriceSeries)).one()
        assert series.pending_price is None
        assert series.pending_count == 0

    def test_a_small_move_is_recorded_but_not_alerted(self, session, hotel_fixture):
        base = datetime.now(UTC)
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture, checked_at=base))
        session.flush()
        summary = ingest_fetch_result(
            session,
            _result(_offer(price="2980")),   # ₹20: below both floors
            _context(hotel_fixture, checked_at=base + timedelta(minutes=30)),
        )
        session.flush()

        assert summary.changes_detected == 0
        assert summary.outcomes[Outcome.INSIGNIFICANT] == 1
        # Still in the history, so the chart shows the wobble.
        assert session.scalar(select(func.count(PriceObservation.id))) == 2

        series = session.scalars(select(PriceSeries)).one()
        # The confirmed baseline holds, so that a run of small drifts is
        # measured against one fixed point and eventually clears the threshold.
        assert series.last_price == Decimal("3000.00")
        # ...but the DISPLAYED price is what the hotel is actually asking. This
        # is the number on the dashboard, and it has to agree with the hotel's
        # own booking page even when the move was too small to alert on.
        assert series.current_price == Decimal("2980.00")

    def test_display_price_tracks_every_check_while_the_baseline_holds(
        self, session, hotel_fixture
    ):
        """A run of sub-threshold drops, none of which ever alerts.

        The regression this pins down: the dashboard read `last_price`, so a
        hotel that walked its rate down in small steps was shown a number it
        had long stopped charging, and the gap only widened -- nothing short of
        one threshold-clearing move ever closed it.
        """
        base = datetime.now(UTC)
        for minutes, price in ((0, "3000"), (30, "2970"), (60, "2945"), (90, "2920")):
            ingest_fetch_result(
                session,
                _result(_offer(price=price)),
                _context(hotel_fixture, checked_at=base + timedelta(minutes=minutes)),
            )
            session.flush()

        series = session.scalars(select(PriceSeries)).one()
        # Each step was under both floors against the 3000 baseline, so nothing
        # was ever confirmed and nobody was told.
        assert session.scalar(select(func.count(PriceChange.id))) == 0
        assert series.last_price == Decimal("3000.00")
        # The screen shows the live rate regardless.
        assert series.current_price == Decimal("2920.00")

    def test_a_sold_out_check_keeps_the_last_known_display_price(
        self, session, hotel_fixture
    ):
        """Availability is carried by `is_available`, not by blanking the rate.

        Wiping it would lose the last known price for as long as the room
        stayed unavailable, and the dashboard would have nothing to show beside
        the "sold out" pill.
        """
        base = datetime.now(UTC)
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture, checked_at=base))
        session.flush()
        ingest_fetch_result(
            session,
            _result(_offer(price=None, available=False)),
            _context(hotel_fixture, checked_at=base + timedelta(minutes=30)),
        )
        session.flush()

        series = session.scalars(select(PriceSeries)).one()
        assert series.is_available is False
        assert series.current_price == Decimal("3000.00")


class TestAvailability:
    def test_sold_out_is_its_own_event_not_a_drop_to_zero(self, session, hotel_fixture):
        base = datetime.now(UTC)
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture, checked_at=base))
        session.flush()

        summary = ingest_fetch_result(
            session,
            _result(_offer(price=None, available=False), sold_out=True),
            _context(hotel_fixture, checked_at=base + timedelta(minutes=30)),
        )
        session.flush()

        change = session.scalars(select(PriceChange)).one()
        assert change.direction.value == "became_unavailable"
        assert change.new_price is None
        assert change.delta_pct is None          # never -100%
        assert change.old_price == Decimal("3000.00")
        assert summary.changes_detected == 1

    def test_a_room_that_disappears_from_the_page_is_marked_unavailable(
        self, session, hotel_fixture
    ):
        """Otherwise a sold-out room freezes at its last price forever."""
        base = datetime.now(UTC)
        ingest_fetch_result(
            session,
            _result(_offer(), _offer(name="Deluxe Room", price="4000", meal_plan="Breakfast")),
            _context(hotel_fixture, checked_at=base),
        )
        session.flush()
        assert session.scalar(select(func.count(PriceSeries.offer_key))) == 2

        # Only the room-only rate comes back this time.
        ingest_fetch_result(
            session,
            _result(_offer()),
            _context(hotel_fixture, checked_at=base + timedelta(minutes=30)),
        )
        session.flush()

        unavailable = session.scalars(
            select(PriceSeries).where(PriceSeries.is_available.is_(False))
        ).all()
        assert len(unavailable) == 1
        assert unavailable[0].meal_plan == "Breakfast"

    def test_an_empty_broken_fetch_marks_nothing_unavailable(self, session, hotel_fixture):
        """A redesign must never be reported as "every room sold out"."""
        base = datetime.now(UTC)
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture, checked_at=base))
        session.flush()

        ingest_fetch_result(
            session,
            FetchResult(offers=[], sold_out_detected=False),
            _context(hotel_fixture, checked_at=base + timedelta(minutes=30)),
        )
        session.flush()

        series = session.scalars(select(PriceSeries)).one()
        assert series.is_available is True


class TestRoomMatching:
    """A name nothing matches is a NEW room, and gets one automatically.

    This used to queue an ``UnmatchedOffer`` and record no price at all until
    somebody mapped the name by hand. Two things went wrong with that, and the
    second is the expensive one: the room had no price for as long as the queue
    went unread, and the question put to the operator was "which of these
    existing rooms is it?" -- whose obliging answer merges two different rooms
    into one series.

    That is not hypothetical. On a real property "Premium Room (Mahogany)" was
    hand-mapped onto "Deluxe Room (Maple)", after which a night when the site
    happened to show the premium room was published as a price rise on the
    deluxe one.

    The two mistakes are not symmetrical. A split series is a visible duplicate
    anyone can merge afterwards; a merged series is invisible and corrupts the
    history permanently. Creating always splits and never merges.
    """

    def test_an_unknown_room_gets_its_own_room_type_and_a_price(
        self, session, hotel_fixture
    ):
        summary = ingest_fetch_result(
            session,
            _result(_offer(name="Presidential Villa with Private Pool")),
            _context(hotel_fixture),
        )
        session.flush()

        assert summary.offers_unmatched == 1
        assert summary.offers_matched == 0
        # No series, no observation, no guess.
        assert session.scalar(select(func.count(PriceSeries.offer_key))) == 0

        unmatched = session.scalars(select(UnmatchedOffer)).one()
        assert unmatched.raw_room_name == "Presidential Villa with Private Pool"
        assert unmatched.occurrence_count == 1

    def test_repeat_sightings_increment_rather_than_duplicate(self, session, hotel_fixture):
        for _ in range(3):
            ingest_fetch_result(
                session, _result(_offer(name="Mystery Suite")), _context(hotel_fixture)
            )
            session.flush()

        unmatched = session.scalars(select(UnmatchedOffer)).one()
        assert unmatched.occurrence_count == 3

    def test_a_rename_is_queued_with_a_suggestion_rather_than_guessed(
        self, session, hotel_fixture
    ):
        """The rename case: "Deluxe Room" becomes "Deluxe Double Room".

        Resolving this automatically would be convenient, and the matcher
        deliberately refuses. The shape of the change -- one qualifier added --
        is identical to what separates "Deluxe Double Occupancy" from "Super
        Deluxe Double Occupancy": two different rooms at very different rates,
        which collapsed into a single price series on a real property until
        score_similarity() started taking the minimum of two ratios.

        The numbers leave no room to have it both ways. The rename scores 63.2
        against the stored canonical name and the sibling scores 88.5, so any
        threshold that accepted the first would also merge the second. Both go
        to a person.

        What the pipeline owes the operator is therefore not a guess but a
        one-click mapping, which is what this pins: the offer is queued WITH
        the room type it most likely belongs to, and nothing is written to the
        price history until someone confirms it.
        """
        summary = ingest_fetch_result(
            session, _result(_offer(name="Deluxe Double Room")), _context(hotel_fixture)
        )
        session.flush()

        assert summary.offers_matched == 0
        assert summary.offers_unmatched == 1

        unmatched = session.scalars(select(UnmatchedOffer)).one()
        assert unmatched.raw_room_name == "Deluxe Double Room"
        # The near miss is carried, so the dashboard can offer the mapping.
        assert unmatched.suggested_room_type_id == hotel_fixture["room"].id
        assert 0.60 <= float(unmatched.suggested_confidence) < 0.90

        # Nothing enters the price history on a guess.
        assert session.scalar(select(func.count(PriceSeries.offer_key))) == 0
        assert session.scalar(select(func.count(RoomTypeAlias.id))) == 0


class TestIdempotency:
    def test_replaying_a_task_writes_no_duplicate_observation(self, session, hotel_fixture):
        """Celery redelivers on worker loss; the pipeline must absorb that."""
        checked_at = datetime.now(UTC)
        context = _context(hotel_fixture, checked_at=checked_at)

        ingest_fetch_result(session, _result(_offer()), context)
        session.flush()
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture,
                                                                checked_at=checked_at))
        session.flush()

        assert session.scalar(select(func.count(PriceObservation.id))) == 1


class TestMealPlanIdentity:
    def test_different_meal_plans_are_different_series(self, session, hotel_fixture):
        """The core guarantee: mismatched conditions cannot be compared.

        A room-only rate and a breakfast-inclusive rate for the same room are
        different offers. They land in different rows, so a change can never be
        manufactured by comparing one against the other.
        """
        ingest_fetch_result(
            session,
            _result(
                _offer(price="3000", meal_plan="Room Only"),
                _offer(price="3400", meal_plan="Breakfast Included"),
            ),
            _context(hotel_fixture),
        )
        session.flush()

        series = session.scalars(select(PriceSeries)).all()
        assert len(series) == 2
        assert {s.last_price for s in series} == {Decimal("3000.00"), Decimal("3400.00")}
        assert session.scalar(select(func.count(PriceChange.id))) == 0


class TestBasisChange:
    """Switching the comparison basis must not invent a price movement.

    A room quoted "₹999 + ₹49.95 taxes" is ₹999 exclusive and ₹1,048.95
    inclusive — one price, two components. Comparing the new component against
    the stored one would read as a 4.8% drop on every room in the portfolio at
    once, which is both wrong and the exact shape of an alert people learn to
    ignore.
    """

    def _both_components(self, name="Deluxe Room"):
        return NormalizedOffer(
            raw_room_name=name,
            price_inclusive=Decimal("1048.95"),
            price_exclusive=Decimal("999.00"),
            taxes_fees=Decimal("49.95"),
            currency="INR",
        )

    def test_rebases_silently_and_records_no_change(self, session, hotel_fixture):
        offer = self._both_components()
        ingest_fetch_result(session, _result(offer), _context(hotel_fixture))
        session.flush()

        series = session.execute(select(PriceSeries)).scalar_one()
        assert series.last_price == Decimal("1048.95")
        assert series.last_price_basis is PriceBasis.INCLUSIVE

        summary = ingest_fetch_result(
            session,
            _result(offer),
            _context(hotel_fixture, price_basis=PriceBasis.EXCLUSIVE),
        )
        session.flush()

        # The stored price is now the one the booking page prints...
        series = session.execute(select(PriceSeries)).scalar_one()
        assert series.last_price == Decimal("999.00")
        assert series.last_price_basis is PriceBasis.EXCLUSIVE
        # ...and nobody was told the price fell.
        assert summary.changes_detected == 0
        assert session.execute(select(func.count(PriceChange.id))).scalar_one() == 0

    def test_next_check_compares_normally_again(self, session, hotel_fixture):
        """The rebase is one check, not a permanent amnesia."""
        offer = self._both_components()
        ingest_fetch_result(session, _result(offer), _context(hotel_fixture))
        session.flush()
        ingest_fetch_result(
            session, _result(offer), _context(hotel_fixture, price_basis=PriceBasis.EXCLUSIVE)
        )
        session.flush()

        later = datetime.now(UTC) + timedelta(hours=1)
        dearer = NormalizedOffer(
            raw_room_name="Deluxe Room",
            price_inclusive=Decimal("1574.95"),
            price_exclusive=Decimal("1499.00"),
            taxes_fees=Decimal("75.95"),
            currency="INR",
        )
        for offset, ctx_time in enumerate((later, later + timedelta(hours=1))):
            summary = ingest_fetch_result(
                session,
                _result(dearer),
                _context(
                    hotel_fixture, checked_at=ctx_time, price_basis=PriceBasis.EXCLUSIVE
                ),
            )
            session.flush()

        # Confirmed on the second sighting, and measured on the new basis:
        # 999 -> 1499, not 1048.95 -> 1499.
        assert summary.changes_detected == 1
        change = session.execute(select(PriceChange)).scalar_one()
        assert change.old_price == Decimal("999.00")
        assert change.new_price == Decimal("1499.00")

    def test_carry_over_does_not_fire_across_a_basis_change(
        self, session, hotel_fixture
    ):
        """The other door into the same false alarm.

        A stay date already priced on the old basis is a different series, and
        a past one never rebases itself because nobody checks yesterday again.
        The carry-over comparison has no debounce, so an unguarded mismatch
        publishes the tax component as a price drop immediately.
        """
        yesterday = StayWindow(
            STAY.check_in - timedelta(days=1), STAY.check_out - timedelta(days=1)
        )
        old_ctx = _context(hotel_fixture)
        ingest_fetch_result(
            session,
            _result(self._both_components()),
            replace(old_ctx, stay=yesterday),
        )
        session.flush()

        summary = ingest_fetch_result(
            session,
            _result(self._both_components()),
            _context(hotel_fixture, price_basis=PriceBasis.EXCLUSIVE),
        )
        session.flush()

        assert summary.change_ids == []
        assert session.execute(select(func.count(PriceChange.id))).scalar_one() == 0


class TestWhenThePriceActuallyChanged:
    """`changed_at` is when the debounce finished, which is a different fact.

    A change is written on its second consecutive sighting, so on a 30-minute
    target the recorded time can be an hour after the hotel moved its rate.
    Showing only that answers "when did we finish checking". `first_seen_at`
    carries the moment the new price appeared.
    """

    def test_first_seen_precedes_the_confirmation(self, session, hotel_fixture):
        base = datetime.now(UTC)
        ingest_fetch_result(
            session, _result(_offer(price="3000")), _context(hotel_fixture, checked_at=base)
        )
        session.flush()

        seen_at = base + timedelta(minutes=30)
        ingest_fetch_result(
            session, _result(_offer(price="3500")), _context(hotel_fixture, checked_at=seen_at)
        )
        session.flush()
        assert session.scalar(select(func.count(PriceChange.id))) == 0, "not yet confirmed"

        confirmed_at = base + timedelta(minutes=60)
        ingest_fetch_result(
            session,
            _result(_offer(price="3500")),
            _context(hotel_fixture, checked_at=confirmed_at),
        )
        session.flush()

        change = session.scalars(select(PriceChange)).one()
        assert change.changed_at == confirmed_at
        assert change.first_seen_at == seen_at
        # The gap is the whole point: half an hour of it here.
        assert change.changed_at - change.first_seen_at == timedelta(minutes=30)

    def test_it_holds_when_confirmation_takes_three_checks(self, session, hotel_fixture):
        """The first sighting must not slide forward with each check.

        Stamping the pending time on every check reads correctly at
        confirm_checks=2, where there is only one pending check to get wrong.
        At three it moves one check at a time, and the recorded first sighting
        is wrong by exactly the amount that still looks plausible.
        """
        base = datetime.now(UTC)
        ctx = lambda t: _context(hotel_fixture, checked_at=t, confirm_checks=3)  # noqa: E731

        ingest_fetch_result(session, _result(_offer(price="3000")), ctx(base))
        session.flush()

        seen_at = base + timedelta(minutes=10)
        for offset in (10, 20, 30):
            ingest_fetch_result(
                session, _result(_offer(price="3500")), ctx(base + timedelta(minutes=offset))
            )
            session.flush()

        change = session.scalars(select(PriceChange)).one()
        assert change.first_seen_at == seen_at, "the first sighting slid forward"
        assert change.changed_at == base + timedelta(minutes=30)

    def test_a_room_disappearing_has_no_earlier_sighting(self, session, hotel_fixture):
        """Reported on the check that finds it gone, with no debounce.

        The page has to SAY it is sold out. An empty result with no such marker
        is a broken read, and the sweep deliberately ignores it rather than
        reporting a redesign as every room selling out at once.
        """
        base = datetime.now(UTC)
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture, checked_at=base))
        session.flush()

        gone_at = base + timedelta(minutes=30)
        ingest_fetch_result(
            session, _result(sold_out=True), _context(hotel_fixture, checked_at=gone_at)
        )
        session.flush()

        change = session.scalars(select(PriceChange)).one()
        assert change.direction is ChangeDirection.BECAME_UNAVAILABLE
        assert change.first_seen_at == change.changed_at == gone_at


class TestOffersThatCollapseIntoOneRoom:
    """Several rooms arriving under one name is a broken selector, not a fetch.

    A six-room property was monitored as a single room for weeks because its
    room_name selector had landed on an amenity chip reading "King Size Bed" on
    every card. Every offer then computed the same offer key, five were dropped
    to protect the transaction, and the check run recorded six offers found,
    zero unmatched, and success.

    Dropping them is still correct -- the offer key is the primary key of
    price_series, so writing both would abort the fetch and lose the rooms that
    were fine. What was missing is that anyone ever heard about it.
    """

    def test_the_drop_is_counted_and_the_names_kept(self, session, hotel_fixture):
        summary = ingest_fetch_result(
            session,
            _result(
                _offer(name="Deluxe Room", price="3000"),
                _offer(name="Deluxe Room", price="4200"),
                _offer(name="Deluxe Room", price="5100"),
            ),
            _context(hotel_fixture),
        )
        session.flush()

        assert summary.offers_seen == 3
        # All three RESOLVED to a room type -- offers_matched counts the match,
        # not the write -- which is precisely why it cannot be used to notice
        # this. It reads three on a fetch that stored one.
        assert summary.offers_matched == 3
        # Not unmatched either: they matched a room perfectly well. They
        # matched the SAME one, which is a different failure needing a
        # different message.
        assert summary.offers_unmatched == 0
        assert summary.offers_collapsed == 2
        assert summary.collapsed_names == ["Deluxe Room"]

        # Exactly one series, as before. The guard still protects the write.
        assert session.scalar(select(func.count(PriceSeries.offer_key))) == 1

    def test_a_clean_fetch_reports_no_collapse(self, session, hotel_fixture):
        """The counter must stay at zero on the ordinary path, or the error it
        feeds becomes noise on Attention and stops being read."""
        summary = ingest_fetch_result(
            session, _result(_offer()), _context(hotel_fixture)
        )
        session.flush()

        assert summary.offers_collapsed == 0
        assert summary.collapsed_names == []


class TestTheNeedsMappingQueueClearsItself:
    """An offer that starts matching must retire its own queue entry.

    Nothing used to close these, so the queue only ever grew. A name is queued
    when no room type matches it; once a matching room type exists -- an
    operator adding one, or the pipeline seeding a hotel's rooms after an
    automatic repair -- the offer resolves cleanly and its prices are recorded
    correctly, while the row asking a human to map it stays open forever.

    That is how six rooms came to sit on the Attention page under "no
    candidate" while those exact six rooms were already being monitored
    correctly on the hotel page.
    """

    def test_a_queued_name_is_closed_once_it_resolves(self, session, hotel_fixture):
        # Seen once under a name nothing matches: queued for a human.
        ingest_fetch_result(
            session, _result(_offer(name="Garden Villa")), _context(hotel_fixture)
        )
        session.flush()
        queued = session.scalars(select(UnmatchedOffer)).one()
        assert queued.resolved_at is None

        # The room now exists -- however it came to exist.
        session.add(
            RoomType(
                hotel_id=hotel_fixture["hotel"].id,
                name="Garden Villa",
                canonical_name="garden villa",
            )
        )
        session.flush()

        ingest_fetch_result(
            session, _result(_offer(name="Garden Villa")), _context(hotel_fixture)
        )
        session.flush()

        session.refresh(queued)
        assert queued.resolved_at is not None, (
            "the offer resolves now, so the row asking a human to map it is "
            "work that no longer needs doing"
        )

    def test_a_name_that_still_does_not_match_stays_queued(self, session, hotel_fixture):
        """Only the row for the name that actually resolved is closed."""
        ingest_fetch_result(
            session, _result(_offer(name="Garden Villa")), _context(hotel_fixture)
        )
        session.flush()

        # A different offer resolving must not clear someone else's row.
        ingest_fetch_result(session, _result(_offer()), _context(hotel_fixture))
        session.flush()

        still_open = session.scalars(
            select(UnmatchedOffer).where(UnmatchedOffer.resolved_at.is_(None))
        ).all()
        assert [row.raw_room_name for row in still_open] == ["Garden Villa"]
