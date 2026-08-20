"""Why a change told nobody, and who hears when the monitor stops.

Two silences this pins down, both of which used to look identical to success:

**A change that reached no one.** ``dispatch_changes`` must set ``notified``
even when there is nobody to tell, or the change reappears in every sweep
forever. That made "delivered" and "reached nobody" the same row afterwards, on
an installation with no recipients as much as on a healthy one. The reason is
now recorded, and the three reasons need three different fixes -- so the tests
insist they stay distinguishable.

**A monitor that stopped.** ``alert_on_silence`` found stale targets and wrote
a log line. Nobody reads log lines. It now sends, which means it also has to
NOT send ninety-six times a day for one ongoing outage.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace


from app.db.models.price import (
    SUPPRESSED_BELOW_THRESHOLD,
    SUPPRESSED_NO_RECIPIENTS,
    SUPPRESSED_RECIPIENT_INACTIVE,
    SUPPRESSION_LABELS,
)
from app.notifications.digest import dedupe_key, ops_dedupe_key
from app.workers import tasks_notify

from tests.unit.test_email_delivery import (
    NOW,
    FakeSession,
    RecordingProvider,
    _install,
    build_world,
    sent_notifications,
)


def dispatch(monkeypatch, session):
    _install(monkeypatch, session)
    tasks_notify.dispatch_changes([99])
    return session.tables["changes"][0]


class TestWhyNobodyWasTold:
    """Three ways to reach zero people, told apart."""

    def test_a_delivered_change_records_no_suppression(self, monkeypatch):
        change = dispatch(monkeypatch, build_world())
        assert change.notified is True
        assert change.suppressed_reason is None

    def test_no_assignment_at_all_says_so(self, monkeypatch):
        """The state a freshly registered recipient is actually in.

        Creating a ``recipients`` row tells nobody anything: the dispatcher
        reads ``hotel_recipients``. Before this, the change was simply marked
        notified and the page reported the work as finished.
        """
        session = build_world()
        session.tables["links"] = []

        change = dispatch(monkeypatch, session)
        assert change.notified is True
        assert change.suppressed_reason == SUPPRESSED_NO_RECIPIENTS

    def test_a_deactivated_assignment_is_not_a_threshold_problem(self, monkeypatch):
        """Different cause, different fix: reactivate, do not lower anything."""
        session = build_world(link_active=False)

        change = dispatch(monkeypatch, session)
        assert change.suppressed_reason == SUPPRESSED_RECIPIENT_INACTIVE

    def test_a_change_everyone_filtered_out_blames_the_threshold(self, monkeypatch):
        """₹300 against a ₹500 floor. Nothing is broken; nothing was sent."""
        session = build_world(min_delta_abs=Decimal("500"))

        change = dispatch(monkeypatch, session)
        assert change.notified is True
        assert change.suppressed_reason == SUPPRESSED_BELOW_THRESHOLD
        assert sent_notifications(session) == []

    def test_the_three_reasons_stay_distinct(self):
        """A shared label would put the fixes back in one bucket."""
        assert len(set(SUPPRESSION_LABELS.values())) == 3


class TestSuppressionIsClearedOnRedelivery:
    def test_a_previously_suppressed_change_that_now_sends_is_not_still_flagged(
        self, monkeypatch
    ):
        """Assign somebody, dispatch again, and the row stops accusing them.

        Only reachable by hand today -- a change is dispatched once -- but the
        field must describe the current outcome rather than the first one, or a
        future "send it anyway" leaves a permanently wrong reason behind.
        """
        session = build_world()
        session.tables["changes"][0].suppressed_reason = SUPPRESSED_NO_RECIPIENTS

        change = dispatch(monkeypatch, session)
        assert change.suppressed_reason is None


class TestOpsAlertsAreNotPriceAlerts:
    def test_an_ops_key_never_collides_with_a_digest_key(self):
        """Both live under one unique index.

        An ops alert has no change ids. Hashed through the ordinary key that is
        the empty list for every one of them, so the first would be written and
        every later one silently deduplicated away -- including the one about a
        different outage next month.
        """
        assert ops_dedupe_key(1, "email", "stale|2026-08-20|11") != dedupe_key(1, "email", [])

    def test_the_same_problem_on_the_same_day_is_one_message(self):
        token = "stale|2026-08-20|11,14"
        assert ops_dedupe_key(1, "email", token) == ops_dedupe_key(1, "email", token)

    def test_a_new_target_going_quiet_does_interrupt(self):
        """Membership is in the token, so a widening outage is not swallowed."""
        assert ops_dedupe_key(1, "email", "stale|2026-08-20|11") != ops_dedupe_key(
            1, "email", "stale|2026-08-20|11,14"
        )

    def test_tomorrow_is_a_new_message(self):
        assert ops_dedupe_key(1, "email", "stale|2026-08-20|11") != ops_dedupe_key(
            1, "email", "stale|2026-08-21|11"
        )

    def test_two_people_get_their_own_key(self):
        token = "stale|2026-08-20|11"
        assert ops_dedupe_key(1, "email", token) != ops_dedupe_key(2, "email", token)


def ops_session(**overrides):
    """A world with no price changes -- only people who might be told."""
    contact = SimpleNamespace(
        id=1,
        name="Ops",
        email=overrides.get("email", "ops@example.com"),
        phone_e164=overrides.get("phone"),
        timezone="Asia/Kolkata",
        is_active=overrides.get("active", True),
        receives_ops_alerts=overrides.get("opted_in", True),
        quiet_hours_start=None,
        quiet_hours_end=None,
    )
    session = FakeSession(recipients=[contact])
    # The real query filters on both flags; FakeSession only knows is_active.
    session.tables["recipients"] = [
        c for c in [contact] if c.receives_ops_alerts
    ]
    return session


class TestNotifyOps:
    def _run(self, monkeypatch, session, channels=("email",)):
        provider = RecordingProvider()
        monkeypatch.setattr(tasks_notify.registry, "get_provider", lambda c: provider)
        monkeypatch.setattr(
            tasks_notify.registry, "available_channels", lambda: list(channels)
        )
        return tasks_notify.notify_ops(
            session, subject="Monitoring has gone quiet", body="details", token="t1", now=NOW
        )

    def test_an_opted_in_contact_is_told(self, monkeypatch):
        session = ops_session()
        created = self._run(monkeypatch, session)

        assert len(created) == 1
        row = session.tables["notifications"][0]
        assert row.channel == "email"
        assert row.subject == "Monitoring has gone quiet"

    def test_an_ops_alert_belongs_to_no_hotel(self, monkeypatch):
        """It is about the system, not a property. A hotel_id here would put
        it in that hotel's delivery history and imply a price moved."""
        session = ops_session()
        self._run(monkeypatch, session)

        row = session.tables["notifications"][0]
        assert row.hotel_id is None
        assert row.price_change_ids == []

    def test_nobody_opted_in_means_nobody_is_told(self, monkeypatch):
        """And is worth a warning: this deployment cannot report its own death."""
        session = ops_session(opted_in=False)
        assert self._run(monkeypatch, session) == []

    def test_a_contact_with_no_usable_channel_is_skipped_not_crashed(self, monkeypatch):
        """Email-only deployment, contact with only a phone number."""
        session = ops_session(email=None, phone="+919876543210")
        assert self._run(monkeypatch, session, channels=("email",)) == []

    def test_whatsapp_is_used_when_that_is_all_there_is(self, monkeypatch):
        session = ops_session(email=None, phone="+919876543210")
        created = self._run(monkeypatch, session, channels=("whatsapp",))

        assert len(created) == 1
        assert session.tables["notifications"][0].channel == "whatsapp"

    def test_email_wins_when_both_are_possible(self, monkeypatch):
        """A list of hostnames and timestamps reads badly in a chat bubble."""
        session = ops_session(email="ops@example.com", phone="+919876543210")
        self._run(monkeypatch, session, channels=("email", "whatsapp"))

        assert session.tables["notifications"][0].channel == "email"


class TestSilenceMessage:
    """What the alert actually says. It has to be actionable from the phone."""

    def _stale(self, **overrides):
        return SimpleNamespace(
            id=overrides.get("id", 11),
            hotel_source_id=1,
            interval_minutes=30,
            last_success_at=overrides.get(
                "last_success", NOW - timedelta(hours=6)
            ),
            circuit_state="closed",
        )

    def _session(self):
        session = FakeSession()
        session.tables["hotel_sources"] = []
        session.get = lambda model, pk: {  # noqa: ARG005
            "HotelSource": SimpleNamespace(id=1, hotel_id=7),
            "Hotel": SimpleNamespace(id=7, name="Kurinji Stay Inn"),
        }.get(model.__name__)
        return session

    def test_it_names_the_hotel_not_the_target_id(self):
        from app.workers.tasks_maintenance import _silence_message

        subject, body = _silence_message(self._session(), [self._stale()], NOW)
        assert "Kurinji Stay Inn" in body
        assert "target 11" not in body

    def test_it_says_how_long_it_has_been_quiet(self):
        from app.workers.tasks_maintenance import _silence_message

        _, body = _silence_message(self._session(), [self._stale()], NOW)
        assert "6h ago" in body

    def test_a_target_that_never_worked_says_so_rather_than_showing_a_huge_number(self):
        from app.workers.tasks_maintenance import _silence_message

        _, body = _silence_message(
            self._session(), [self._stale(last_success=None)], NOW
        )
        assert "never succeeded" in body

    def test_the_subject_counts_the_sources(self):
        from app.workers.tasks_maintenance import _silence_message

        subject, _ = _silence_message(
            self._session(), [self._stale(id=11), self._stale(id=14)], NOW
        )
        assert "2 sources" in subject

    def test_one_source_is_not_called_1_sources(self):
        from app.workers.tasks_maintenance import _silence_message

        subject, _ = _silence_message(self._session(), [self._stale()], NOW)
        assert "1 source" in subject and "1 sources" not in subject


class TestHeartbeat:
    """The only alarm that works when this process is the thing that broke."""

    def test_it_is_a_no_op_when_no_watchdog_is_configured(self, monkeypatch):
        from app.workers import tasks_maintenance

        monkeypatch.setattr(
            tasks_maintenance, "get_settings",
            lambda: SimpleNamespace(heartbeat_url="", heartbeat_timeout_seconds=10.0),
        )
        assert tasks_maintenance.heartbeat()["status"] == "disabled"

    def test_a_sick_database_does_not_ping(self, monkeypatch):
        """Pinging on a dead database is monitoring that lies -- the watchdog
        stays green while nothing works, which is worse than no watchdog."""
        from contextlib import contextmanager

        from app.workers import tasks_maintenance

        monkeypatch.setattr(
            tasks_maintenance, "get_settings",
            lambda: SimpleNamespace(
                heartbeat_url="https://hc.example/ping", heartbeat_timeout_seconds=10.0
            ),
        )

        @contextmanager
        def broken_session():
            raise RuntimeError("could not connect")
            yield

        monkeypatch.setattr(tasks_maintenance, "sync_session", broken_session)
        assert tasks_maintenance.heartbeat()["status"] == "db_unreachable"
