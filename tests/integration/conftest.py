"""Fixtures for tests that need a real PostgreSQL.

Skipped by default. The models use JSONB, native enums, arrays, ``ON CONFLICT``
and a partitioned table — none of which SQLite can stand in for, and a fake
that got any of them subtly wrong would be worse than no test at all.

Run them against a live database with::

    docker compose up -d postgres
    TEST_DATABASE_URL=postgresql+psycopg://hotelmonitor_app:...@localhost:5432/hotelmonitor_test \\
        python -m pytest -m integration

Each test runs inside a transaction that is rolled back afterwards, so the
suite is re-runnable and leaves nothing behind.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def engine():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests need PostgreSQL")

    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)

    from app.db.base import Base
    from app.db import models  # noqa: F401 - registers every model on the metadata

    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # The declarative metadata, not `alembic upgrade head`: this asserts the
    # MODELS are coherent. Whether the migration matches them is a separate
    # question, checked by running the migration itself on first deploy.
    # DROPPED FIRST, then recreated.
    #
    # ``create_all`` creates missing tables and never alters an existing one,
    # so a column added to a model simply did not appear in a database created
    # by an earlier run. The tests then failed with 'column "first_seen_at" of
    # relation "price_changes" does not exist' -- which reads like a broken
    # migration and is in fact a stale test database, several minutes of
    # confusion away from the truth.
    #
    # The name check is the safety rail that makes dropping acceptable: this
    # fixture destroys every table it knows about, and the one thing that must
    # never happen is someone pointing TEST_DATABASE_URL at real data and
    # running the suite.
    database = (engine.url.database or "").lower()
    if "test" not in database:
        pytest.exit(
            f"TEST_DATABASE_URL points at {database!r}. These tests DROP every "
            f"table before recreating them, so the database name must contain "
            f"'test'. Refusing to run.",
            returncode=1,
        )

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _create_observation_partitions(engine)

    yield engine
    engine.dispose()


def _create_observation_partitions(engine) -> None:
    """``price_observations`` is partitioned, so it needs partitions to exist.

    Without one covering "now", every insert fails with "no partition of
    relation found for row" — which is exactly the production failure the
    ``maintenance.ensure_partitions`` task prevents.
    """
    from sqlalchemy import text

    from app.workers.tasks_maintenance import _add_months

    # ``create_all`` builds the tables from the models but not the plpgsql
    # helper the migration installs, so the DDL is issued directly here. The
    # bounds match what that function produces.
    today = date.today().replace(day=1)
    with engine.begin() as connection:
        for offset in (-1, 0, 1):
            start = _add_months(today, offset)
            end = _add_months(start, 1)
            connection.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS price_observations_{start:%Y_%m} "
                    f"PARTITION OF price_observations "
                    f"FOR VALUES FROM ('{start:%Y-%m-%d}') TO ('{end:%Y-%m-%d}')"
                )
            )


@pytest.fixture
def session(engine):
    """A session whose work is rolled back at the end of the test."""
    from sqlalchemy.orm import Session

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def hotel_fixture(session):
    """A hotel with one source, one room type, and one monitor target.

    The minimum arrangement that can produce a price: everything the ingest
    pipeline needs to resolve a room name and compute an offer key.
    """
    from app.db.models import (
        DateStrategy,
        Hotel,
        HotelSource,
        MonitorTarget,
        RoomType,
        Source,
    )

    hotel = Hotel(name="Test Resort", slug="test-resort", location="Yelagiri")
    session.add(hotel)
    session.flush()

    source = Source(
        code="direct-test",
        display_name="Test booking engine",
        adapter_key="http_json",
        base_domain="book.example.test",
        is_enabled=True,
        tos_reviewed_at=date(2026, 8, 1),
        tos_reviewed_by="Integration Test",
    )
    session.add(source)
    session.flush()

    hotel_source = HotelSource(
        hotel_id=hotel.id, source_id=source.id, url="https://book.example.test/x"
    )
    session.add(hotel_source)
    session.flush()

    room = RoomType(
        hotel_id=hotel.id, name="Deluxe Room", canonical_name="deluxe", capacity=2
    )
    session.add(room)

    target = MonitorTarget(
        hotel_source_id=hotel_source.id,
        date_strategy=DateStrategy.FIXED,
        fixed_check_in=date(2026, 12, 20),
        fixed_check_out=date(2026, 12, 21),
        next_run_at=datetime.now(UTC),
    )
    session.add(target)
    session.flush()

    return {
        "hotel": hotel,
        "source": source,
        "hotel_source": hotel_source,
        "room": room,
        "target": target,
    }
