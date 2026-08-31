"""A recipient with an email address gets the price-change email.

The question this file answers is the one nobody can answer by reading the
code: a person is registered with an address, a price moves, does a message
actually reach them? Five things have to line up -- an assignment, an active
recipient, the email channel on that assignment, a threshold that lets the
change through, and a provider that accepts it -- and any one of them failing
is silent. Silence is indistinguishable from "no prices changed today".

The task bodies normally need PostgreSQL (see tests/integration). The fake
session here is deliberately dumb: it serves whole tables by entity and
ignores WHERE clauses, so it can say nothing about SQL correctness -- that is
the integration suite's job. What it does check is the decision path between a
PriceChange row and provider.send(), which is where the five conditions live.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.db.models import (
    Hotel,
    HotelRecipient,
    Notification,
    NotificationStatus,
    PriceChange,
    PriceSeries,
    Recipient,
    RoomType,
)
from app.notifications.base import SendResult
from app.workers import tasks_notify

NOW = datetime(2026, 8, 20, 15, 30, tzinfo=UTC)


# -- the fake database ------------------------------------------------
class _Scalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return _Scalars(self._rows)


class FakeSession:
    """Serves rows by entity. Filters are ignored -- see the module docstring."""

    def __init__(self, **tables):
        self.tables = tables
        self.added = []
        self.flushes = 0

    def execute(self, statement):
        columns = statement.column_descriptions
        entity = columns[0]["entity"]

        # _all_hotel_recipient_ids asks for bare ids, not rows. Returning
        # Recipient objects here puts them where recipient IDS are expected,
        # and the next query fails deep inside SQLAlchemy rather than here.
        if entity is Recipient and len(columns) == 1 and columns[0]["name"] == "id":
            return _Result(
                [
                    r.id
                    for r in self.tables.get("recipients", [])
                    if r.is_active and getattr(r, "alerts_all_hotels", False)
                ]
            )

        # _assignments_for asks for two columns; _links_by_pair for the row.
        if entity is HotelRecipient and len(columns) == 2:
            return _Result(
                [
                    (link.hotel_id, link.recipient_id)
                    for link in self.tables.get("links", [])
                    if link.is_active
                ]
            )
        for name, model in (
            ("changes", PriceChange), ("links", HotelRecipient),
            ("recipients", Recipient), ("hotels", Hotel),
            ("series", PriceSeries), ("rooms", RoomType),
        ):
            if entity is model:
                rows = self.tables.get(name, [])
                if model is Recipient:
                    # The real query filters on is_active; an inactive person
                    # must not be found, or the test proves nothing.
                    rows = [r for r in rows if r.is_active]
                return _Result(rows)
        raise AssertionError(f"FakeSession has no table for {entity}")

    def get(self, model, pk):
        for name in ("notifications", "recipients", "changes", "hotels"):
            for row in self.tables.get(name, []):
                if isinstance(row, model) and row.id == pk:
                    return row
        return None

    def add(self, obj):
        self.added.append(obj)
        self.tables.setdefault("notifications", []).append(obj)

    def flush(self):
        self.flushes += 1
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = index
            # Column defaults land at flush time in a real session; without
            # this, attempts is None and `attempts += 1` raises in the sender.
            if isinstance(obj, Notification) and obj.attempts is None:
                obj.attempts = 0

    @contextmanager
    def begin_nested(self):
        yield self


class RecordingProvider:
    """Stands in for SMTP. Records what it was handed."""

    channel = "email"
    provider_name = "smtp"

    def __init__(self, result=None):
        self.sent = []
        self.result = result or SendResult(ok=True, provider_message_id="msg-1")

    def is_configured(self):
        return True

    def send(self, destination, message):
        self.sent.append((destination, message))
        return self.result


# -- the world a price change happens in -------------------------------
def build_world(**overrides):
    hotel = Hotel(id=7, name="Sunrise Resort")
    recipient = Recipient(
        id=1,
        name="Priya",
        email=overrides.get("email", "priya@example.com"),
        phone_e164=None,
        timezone="Asia/Kolkata",
        is_active=overrides.get("recipient_active", True),
        quiet_hours_start=overrides.get("quiet_start"),
        quiet_hours_end=overrides.get("quiet_end"),
    )
    link = HotelRecipient(
        id=1,
        hotel_id=7,
        recipient_id=1,
        channels=overrides.get("channels", ["email"]),
        min_delta_abs=overrides.get("min_delta_abs"),
        min_delta_pct=None,
        is_active=overrides.get("link_active", True),
    )
    change = PriceChange(
        id=99,
        hotel_id=7,
        offer_key="offer-1",
        old_price=Decimal("3000"),
        new_price=Decimal("2700"),
        delta=Decimal("-300"),
        delta_pct=Decimal("-10.00"),
        currency="INR",
        direction="decrease",
        previous_offer_key=None,
        notified=False,
    )
    series = PriceSeries(
        offer_key="offer-1",
        room_type_id=3,
        check_in=date(2026, 9, 5),
        check_out=date(2026, 9, 6),
        meal_plan="Breakfast Included",
    )
    room = RoomType(id=3, hotel_id=7, name="Deluxe Room")
    return FakeSession(
        hotels=[hotel], recipients=[recipient], links=[link],
        changes=[change], series=[series], rooms=[room],
    )


@pytest.fixture
def world(monkeypatch):
    """One session shared by dispatch and send, as the database would be."""
    session = build_world()
    _install(monkeypatch, session)
    return session


def _install(monkeypatch, session, provider=None):
    @contextmanager
    def fake_sync_session():
        yield session

    monkeypatch.setattr(tasks_notify, "sync_session", fake_sync_session)
    # No broker in a unit test: record the hand-off instead of enqueuing it.
    monkeypatch.setattr(
        tasks_notify.send_notification, "apply_async",
        lambda *args, **kwargs: session.tables.setdefault("queued", []).append(args),
    )
    provider = provider or RecordingProvider()
    monkeypatch.setattr(tasks_notify.registry, "get_provider", lambda channel: provider)
    # Redis is not running; the quota is not what these tests are about.
    monkeypatch.setattr(tasks_notify, "recipient_quota_remaining", lambda *a: 10)
    monkeypatch.setattr(tasks_notify, "consume_recipient_quota", lambda *a: None)
    session.provider = provider
    return provider


def sent_notifications(session):
    return [n for n in session.tables.get("notifications", [])]


class TestTheHappyPath:
    """One recipient, one address, one assignment, one price drop."""

    def test_a_queued_email_is_created_for_the_recipient(self, world):
        tasks_notify.dispatch_changes([99])

        rows = sent_notifications(world)
        assert len(rows) == 1
        assert rows[0].channel == "email"
        assert rows[0].recipient_id == 1
        assert rows[0].status is NotificationStatus.QUEUED

    def test_the_change_is_marked_notified_so_it_is_not_sent_twice(self, world):
        tasks_notify.dispatch_changes([99])
        assert world.tables["changes"][0].notified is True

    def test_the_message_says_what_changed(self, world):
        tasks_notify.dispatch_changes([99])

        body = sent_notifications(world)[0].body_rendered
        assert "Deluxe Room" in body
        assert "3,000" in body and "2,700" in body
        assert "Sunrise Resort" in sent_notifications(world)[0].subject

    def test_it_reaches_the_address_the_recipient_registered(self, world):
        """The whole point: provider.send is called with their email."""
        tasks_notify.dispatch_changes([99])
        tasks_notify.send_notification(1)

        assert len(world.provider.sent) == 1
        destination, message = world.provider.sent[0]
        assert destination.email == "priya@example.com"
        assert destination.name == "Priya"
        assert "Deluxe Room" in message.text

    def test_a_sent_message_is_recorded_as_sent(self, world):
        tasks_notify.dispatch_changes([99])
        tasks_notify.send_notification(1)

        row = sent_notifications(world)[0]
        assert row.status is NotificationStatus.SENT
        assert row.sent_at is not None
        assert row.provider_message_id == "msg-1"


class TestTheWaysItSilentlyDoesNotArrive:
    """Each of these produces no email. All of them look identical from a
    dashboard that only shows price changes -- which is why they are tested
    one at a time rather than trusted to be obvious."""

    def _run(self, monkeypatch, session):
        _install(monkeypatch, session)
        tasks_notify.dispatch_changes([99])
        return sent_notifications(session)

    def test_no_assignment_means_no_message(self, monkeypatch):
        session = build_world()
        session.tables["links"] = []
        assert self._run(monkeypatch, session) == []

    def test_an_inactive_assignment_means_no_message(self, monkeypatch):
        session = build_world(link_active=False)
        assert self._run(monkeypatch, session) == []

    def test_an_inactive_recipient_means_no_message(self, monkeypatch):
        session = build_world(recipient_active=False)
        assert self._run(monkeypatch, session) == []

    def test_a_change_under_their_floor_means_no_message(self, monkeypatch):
        session = build_world(min_delta_abs=Decimal("500"))
        assert self._run(monkeypatch, session) == []

    def test_whatsapp_only_assignment_sends_no_email(self, monkeypatch):
        session = build_world(channels=["whatsapp"])
        rows = self._run(monkeypatch, session)
        assert [row.channel for row in rows] == ["whatsapp"]

    def test_an_unassigned_hotel_still_marks_the_change_notified(self, monkeypatch):
        """Otherwise it reappears in every dispatch, forever."""
        session = build_world()
        session.tables["links"] = []
        self._run(monkeypatch, session)
        assert session.tables["changes"][0].notified is True


class TestQuietHoursHoldRatherThanDrop:
    def test_a_night_time_change_is_held_for_the_morning(self, monkeypatch):
        """22:00-07:00 IST. Dispatched at 01:00 IST, which is 19:30 UTC."""
        import app.workers.tasks_notify as module

        session = build_world(quiet_start=__import__("datetime").time(22, 0),
                              quiet_end=__import__("datetime").time(7, 0))
        _install(monkeypatch, session)

        class _Night(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 19, 19, 30, tzinfo=UTC)

        monkeypatch.setattr(module, "datetime", _Night)
        module.dispatch_changes([99])

        row = sent_notifications(session)[0]
        assert row.status is NotificationStatus.QUEUED
        # Held, not dropped: it goes out at 07:00 local.
        assert row.scheduled_for is not None
        assert row.scheduled_for.astimezone(UTC).hour == 1  # 07:00 IST
        assert session.tables.get("queued") is None  # nothing sent tonight


class TestFailuresAreRecordedNotLost:
    def test_a_refused_address_is_marked_failed_with_the_reason(self, monkeypatch):
        session = build_world()
        provider = RecordingProvider(
            SendResult(ok=False, error_code="recipient_refused",
                       error_detail="550 no such user", retryable=False)
        )
        _install(monkeypatch, session, provider)

        tasks_notify.dispatch_changes([99])
        tasks_notify.send_notification(1)

        row = sent_notifications(session)[0]
        assert row.status is NotificationStatus.FAILED
        assert row.error_code == "recipient_refused"
        assert "550" in row.error_detail

    def test_an_already_sent_message_is_not_sent_again(self, monkeypatch):
        """A retry that raced the original must not double-send."""
        session = build_world()
        provider = _install(monkeypatch, session)

        tasks_notify.dispatch_changes([99])
        tasks_notify.send_notification(1)
        tasks_notify.send_notification(1)

        assert len(provider.sent) == 1


class TestDeduplication:
    def test_the_same_change_set_keys_the_same_message(self, monkeypatch):
        session = build_world()
        _install(monkeypatch, session)
        tasks_notify.dispatch_changes([99])

        from app.notifications.digest import dedupe_key

        row = sent_notifications(session)[0]
        assert row.dedupe_key == dedupe_key(1, "email", [99])
