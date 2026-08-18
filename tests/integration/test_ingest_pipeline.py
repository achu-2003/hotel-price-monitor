"""The ingest pipeline, against a real PostgreSQL.

This is the code that turns adapter output into stored history and confirmed
changes. The pure decisions inside it are already covered by the comparison and
room-matching unit tests; what needs a real database is everything those tests
cannot see: the ``ON CONFLICT`` upserts, the partitioned insert, ``SELECT FOR
UPDATE`` on the series row, and the disappearance sweep.

Skipped unless ``TEST_DATABASE_URL`` is set — see ``conftest.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.adapters.base import FetchResult, NormalizedOffer
from app.db.models import (
    PriceBasis,
    PriceChange,
    PriceObservation,
    PriceSeries,
    RoomTypeAlias,
    UnmatchedOffer,
)
from app.services.comparison import Outcome, Thresholds
from app.services.dates import StayWindow
from app.services.ingest import IngestContext, ingest_fetch_result

pytestmark = pytest.mark.integration

STAY = StayWindow(datetime(2026, 12, 20).date(), datetime(2026, 12, 21).date())


def _context(fixture, *, checked_at=None, confirm_checks=2) -> IngestContext:
    return IngestContext(
        hotel_id=fixture["hotel"].id,
        source_id=fixture["source"].id,
        hotel_source_id=fixture["hotel_source"].id,
        stay=STAY,
        adults=2,
        children=0,
        currency="INR",
        price_basis=PriceBasis.INCLUSIVE,
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
    def test_an_unknown_room_is_queued_not_guessed(self, session, hotel_fixture):
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

    def test_a_rename_resolves_through_fuzzy_matching_and_is_remembered(
        self, session, hotel_fixture
    ):
        """The rename case: "Deluxe Room" becomes "Deluxe Double Room".

        Without this the series silently splits in two and the price history
        breaks exactly where someone would want to look at it.
        """
        summary = ingest_fetch_result(
            session, _result(_offer(name="Deluxe Double Room")), _context(hotel_fixture)
        )
        session.flush()

        assert summary.offers_matched == 1
        alias = session.scalars(select(RoomTypeAlias)).one()
        assert alias.room_type_id == hotel_fixture["room"].id
        assert alias.match_method.value == "fuzzy"
        # Scoped to the hotel, so the same name on another property is free to
        # mean something else.
        assert alias.hotel_id == hotel_fixture["hotel"].id


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
