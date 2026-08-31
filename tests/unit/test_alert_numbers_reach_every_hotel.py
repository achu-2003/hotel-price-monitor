"""The short list of numbers that gets told about everything.

An alert number is a Recipient wearing two flags rather than a new kind of
thing, which is what lets it reuse the digest, the dedupe key and the delivery
history. The flags carry the two promises the Alerts page makes:

``alerts_all_hotels``  every hotel, including ones added after the number was.
``bypass_throttle``    immediately, at any hour, with no hourly cap.

The first is the one worth testing hardest. Coverage is resolved at dispatch
instead of being written into hotel_recipients, precisely so a hotel created
through some path nobody remembered still reaches these numbers -- and the
failure it prevents is invisible, because a hotel that alerts nobody looks
exactly like a hotel whose price never moved.
"""
from __future__ import annotations

from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest

from app.workers import tasks_notify


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Answers the two queries the assignment helpers make.

    Narrow on purpose: these helpers are pure lookups, and a real session here
    would test SQLAlchemy rather than the coverage rule.
    """

    def __init__(self, links=(), all_hotel_ids=()):
        self._links = list(links)
        self._all_hotel_ids = list(all_hotel_ids)
        self.calls = 0

    def execute(self, statement):
        self.calls += 1
        text = str(statement)
        if "alerts_all_hotels" in text:
            return _FakeResult(self._all_hotel_ids)
        # Two different shapes come off hot_recipients: _assignments_for
        # selects the two id columns and unpacks tuples, while _links_by_pair
        # selects the whole entity. Told apart by the primary key, which only
        # the entity query names.
        if "hotel_recipients.id" in text:
            return _FakeResult(self._links)
        if "hotel_recipients" in text:
            return _FakeResult(
                [(l.hotel_id, l.recipient_id) for l in self._links if l.is_active]
            )
        return _FakeResult([])


def _link(hotel_id, recipient_id, channels=None, is_active=True, min_abs=None):
    return SimpleNamespace(
        hotel_id=hotel_id,
        recipient_id=recipient_id,
        channels=channels if channels is not None else ["email"],
        is_active=is_active,
        min_delta_abs=min_abs,
        min_delta_pct=None,
    )


# ── coverage ─────────────────────────────────────────────────────────


def test_an_alert_number_covers_a_hotel_nobody_assigned_it_to():
    """The promise: every hotel, no assignment required."""
    session = _FakeSession(links=[], all_hotel_ids=[7])

    assignments = tasks_notify._assignments_for(session, {1, 2, 3})

    assert assignments == {1: [7], 2: [7], 3: [7]}


def test_a_hotel_created_later_is_covered_with_no_backfill():
    """The reason coverage is resolved at dispatch rather than materialised.

    Hotel 99 did not exist when the number was added and has no
    hotel_recipients row of any kind. It is still covered, because nothing had
    to remember to write one.
    """
    session = _FakeSession(links=[_link(1, 7)], all_hotel_ids=[7])

    assignments = tasks_notify._assignments_for(session, {1, 99})

    assert 99 in assignments
    assert assignments[99] == [7]


def test_an_alert_number_is_not_batched_twice_for_one_hotel():
    """It may also hold a real assignment; it must still appear once.

    Twice in this list means two digests for the same move, which is two
    billed WhatsApp messages saying the same thing.
    """
    session = _FakeSession(links=[_link(1, 7)], all_hotel_ids=[7])

    assignments = tasks_notify._assignments_for(session, {1})

    assert assignments[1] == [7]


def test_ordinary_recipients_are_untouched_by_the_flag():
    session = _FakeSession(links=[_link(1, 3), _link(2, 4)], all_hotel_ids=[])

    assignments = tasks_notify._assignments_for(session, {1, 2})

    assert assignments == {1: [3], 2: [4]}


def test_an_inactive_alert_number_covers_nothing():
    """Clearing a row on the Alerts page deactivates the recipient.

    The query filters on is_active, so the fake returning no ids is exactly
    what the database would return -- and the hotels fall back to their real
    assignments only.
    """
    session = _FakeSession(links=[_link(1, 3)], all_hotel_ids=[])

    assert tasks_notify._assignments_for(session, {1}) == {1: [3]}


# ── the synthesised assignment ───────────────────────────────────────


def test_the_synthesised_link_sends_on_whatsapp_only():
    """dispatch_changes skips a pair with no link, so one is invented.

    It must be WhatsApp: these are phone numbers, several with no email
    address at all, and the model default is ``["email"]`` -- which would queue
    a message with nowhere to go.
    """
    session = _FakeSession(links=[], all_hotel_ids=[7])

    links = tasks_notify._links_by_pair(session, {1})

    assert links[(1, 7)].channels == ["whatsapp"]
    assert links[(1, 7)].is_active is True


def test_the_synthesised_link_has_no_threshold():
    """Every move, which is the point of an alert number."""
    session = _FakeSession(links=[], all_hotel_ids=[7])

    link = tasks_notify._links_by_pair(session, {1})[(1, 7)]

    assert link.min_delta_abs is None
    assert link.min_delta_pct is None


def test_a_real_assignment_is_never_overridden():
    """Assigning an alert number to one hotel by hand must still mean something.

    Otherwise a threshold or an extra channel set deliberately would be
    silently discarded by the flag.
    """
    real = _link(1, 7, channels=["email", "whatsapp"], min_abs=500)
    session = _FakeSession(links=[real], all_hotel_ids=[7])

    links = tasks_notify._links_by_pair(session, {1, 2})

    assert links[(1, 7)] is real
    assert links[(1, 7)].min_delta_abs == 500
    # ...and the hotel with no row still gets the synthesised one.
    assert links[(2, 7)].channels == ["whatsapp"]


def test_the_synthesised_link_is_never_added_to_the_session():
    """It answers one dispatch and must not become a row.

    Persisting it would freeze today's hotel list into the database and undo
    the future-hotel guarantee the flag exists for.
    """
    session = _FakeSession(links=[], all_hotel_ids=[7])
    session.add = lambda *a, **k: pytest.fail("synthesised link was persisted")

    tasks_notify._links_by_pair(session, {1})


# ── immediacy ────────────────────────────────────────────────────────


def _recipient(**overrides):
    base = dict(
        id=1,
        name="Front office",
        email=None,
        phone_e164="+919876543210",
        timezone="Asia/Kolkata",
        is_active=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        alerts_all_hotels=True,
        bypass_throttle=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


#: Stand-in for the quiet-hours release moment.
_HELD_AT = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)


class _NotificationSession:
    """Enough session to let ``_create_notification`` run to the end."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        obj.id = 1
        self.added.append(obj)

    def begin_nested(self):
        class _Savepoint:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Savepoint()

    def flush(self):
        return None


def _create(monkeypatch, recipient, *, in_quiet: bool):
    """Run the real ``_create_notification`` and hand back the row it wrote."""
    monkeypatch.setattr(tasks_notify, "in_quiet_hours", lambda *a: in_quiet)
    # A real datetime: the held path logs release_at.isoformat().
    monkeypatch.setattr(tasks_notify, "release_time", lambda *a: _HELD_AT)
    monkeypatch.setattr(
        tasks_notify.registry,
        "get_provider",
        lambda channel: SimpleNamespace(provider_name="mydreams"),
    )
    session = _NotificationSession()
    returned = tasks_notify._create_notification(
        session,
        recipient=recipient,
        hotel_id=1,
        channel="whatsapp",
        change_ids=[1],
        subject="Rate update",
        body="Rate update",
        now=tasks_notify.datetime.now(tasks_notify.UTC),
    )
    return returned, session.added[0]


def test_quiet_hours_are_skipped_for_an_alert_number(monkeypatch):
    """3 AM is exactly when this was asked to still fire.

    ``scheduled_for`` staying None is what makes the difference: a row with one
    set is held for the release sweep and returns None, so the caller never
    queues it.
    """
    returned, row = _create(monkeypatch, _recipient(), in_quiet=True)

    assert row.scheduled_for is None
    assert returned == 1


def test_an_ordinary_recipient_still_waits_out_quiet_hours(monkeypatch):
    """The bypass is per recipient, not a switch that disables the feature."""
    ordinary = _recipient(alerts_all_hotels=False, bypass_throttle=False)

    returned, row = _create(monkeypatch, ordinary, in_quiet=True)

    assert row.scheduled_for == _HELD_AT
    assert returned is None


def test_outside_quiet_hours_both_kinds_send_immediately(monkeypatch):
    ordinary = _recipient(alerts_all_hotels=False, bypass_throttle=False)

    returned, row = _create(monkeypatch, ordinary, in_quiet=False)

    assert row.scheduled_for is None
    assert returned == 1


# ── the assignment that swallowed the promise ────────────────────────
#
# THE INCIDENT
# ============
# One number was added on the Alerts page, which reported it as covering all
# ten hotels. It would have reached WhatsApp on exactly ONE of them.
#
# An alert number is usually somebody who is already a recipient, and this one
# had ``channels=['email']`` rows from before for nine of the ten hotels. The
# rule was "a real row always wins" -- meant to protect a hand-set threshold --
# so those nine rows suppressed the synthesised WhatsApp link entirely. Only
# the tenth hotel, which had no prior assignment, would have sent anything.
#
# Nothing reported a problem. The panel said "every price change, on every
# hotel", the page said ten hotels covered, and nine in ten messages would
# simply never have been sent.
class TestWhatsAppIsAddedToAnAssignmentNotBlockedByIt:
    def test_an_existing_email_assignment_gains_whatsapp(self):
        """The regression. This returned ['email'] and sent nothing."""
        session = _FakeSession(links=[_link(1, 7, channels=["email"])], all_hotel_ids=[7])

        link = tasks_notify._links_by_pair(session, {1})[(1, 7)]

        assert "whatsapp" in link.channels
        assert "email" in link.channels

    def test_the_stored_row_is_never_touched(self):
        """A mutation here would rewrite the operator's configuration.

        The real row is attached to the session, so appending to its channels
        marks it dirty and the next commit persists it -- turning a decision
        made for one dispatch into a permanent, invisible edit.
        """
        real = _link(1, 7, channels=["email"])
        session = _FakeSession(links=[real], all_hotel_ids=[7])

        merged = tasks_notify._links_by_pair(session, {1})[(1, 7)]

        assert merged is not real
        assert real.channels == ["email"]

    def test_the_rows_own_threshold_is_carried_across(self):
        """Adding a phone number is not a statement about sensitivity."""
        session = _FakeSession(
            links=[_link(1, 7, channels=["email"], min_abs=500)], all_hotel_ids=[7]
        )

        link = tasks_notify._links_by_pair(session, {1})[(1, 7)]

        assert link.min_delta_abs == 500

    def test_a_row_that_already_sends_whatsapp_is_left_alone(self):
        """Nothing to add, so nothing is copied."""
        real = _link(1, 7, channels=["email", "whatsapp"])
        session = _FakeSession(links=[real], all_hotel_ids=[7])

        assert tasks_notify._links_by_pair(session, {1})[(1, 7)] is real

    def test_a_deactivated_assignment_does_not_veto_the_number(self):
        """Switching one hotel off predates their being an alert number.

        It cannot express "...but still WhatsApp me about it", whereas the flag
        says exactly that.
        """
        session = _FakeSession(
            links=[_link(1, 7, channels=["email"], is_active=False)], all_hotel_ids=[7]
        )

        link = tasks_notify._links_by_pair(session, {1})[(1, 7)]

        assert link.is_active is True
        assert "whatsapp" in link.channels

    def test_a_deactivated_assignment_does_not_quietly_resume_email(self):
        """Only the channel the flag is about comes back."""
        session = _FakeSession(
            links=[_link(1, 7, channels=["email"], is_active=False)], all_hotel_ids=[7]
        )

        link = tasks_notify._links_by_pair(session, {1})[(1, 7)]

        assert link.channels == ["whatsapp"]

    def test_an_ordinary_recipients_assignment_is_untouched(self):
        """The union applies to alert numbers, not to everybody."""
        real = _link(1, 3, channels=["email"])
        session = _FakeSession(links=[real], all_hotel_ids=[])

        assert tasks_notify._links_by_pair(session, {1})[(1, 3)] is real
        assert real.channels == ["email"]

    def test_every_hotel_ends_up_reachable_on_whatsapp(self):
        """The shape of the real failure: nine assigned hotels, one not."""
        assigned = [_link(h, 7, channels=["email"]) for h in range(1, 10)]
        session = _FakeSession(links=assigned, all_hotel_ids=[7])

        links = tasks_notify._links_by_pair(session, set(range(1, 11)))

        reachable = [h for h in range(1, 11) if "whatsapp" in links[(h, 7)].channels]
        assert len(reachable) == 10
