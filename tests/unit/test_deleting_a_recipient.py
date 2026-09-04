"""Removing a person from the alert list for good.

There were two ways to stop telling someone about prices and both kept them:
deactivating silences the row, and dropping a number off the alert-numbers
list deactivates it too. Neither is the answer for a receptionist who has
left, whose name, number and every message ever sent to them otherwise sit in
the settings list forever.

DELETE is that answer, and it is the one operation on this resource that
cannot be undone, so what it destroys is pinned here: the assignments and the
delivery history go with the person -- ``notifications.recipient_id`` is NOT
NULL with ON DELETE CASCADE, so the log cannot be orphaned and kept -- and the
audit entry is written BEFORE the row, because afterwards there is nothing
left to look up.

The route is exercised for real. The database is a stand-in: the point is the
handler's own logic and the shape of the calls it makes, not the SQL. That is
the same reason ``test_alert_numbers_endpoint`` exists -- an audit call with a
missing argument passed every unit test and 500'd on the first click.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import current_user, db_session, require_admin
from app.dashboard.routes import dashboard_user
from app.db.models import Recipient
from app.main import create_app


class _FakeAdmin:
    id = 1
    username = "ops"
    full_name = "Ops"
    is_admin = True
    role = "admin"


class _Session:
    """A recipient to find, two counts to answer, and a DELETE to record."""

    def __init__(self, recipient=None, assignments=0, messages=0):
        self.recipient = recipient
        self.counts = {"hotel_recipients": assignments, "notifications": messages}
        self.deletes = []
        self.audits = []
        self.committed = False

    async def get(self, _model, object_id):
        if self.recipient is not None and self.recipient.id == object_id:
            return self.recipient
        return None

    async def scalar(self, statement):
        # Answered by which table is being counted rather than by call order,
        # so a reordering in the handler cannot silently swap the two numbers
        # in the audit entry -- which is the whole content of that entry.
        sql = str(statement)
        for table, count in self.counts.items():
            if table in sql:
                return count
        return 0

    async def execute(self, statement):
        self.deletes.append(str(statement))
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None


def _recipient(**kwargs):
    fields = {
        "name": "Priya, front office",
        "email": "priya@example.com",
        "phone_e164": "+919876543210",
    }
    fields.update(kwargs)
    recipient = Recipient(**fields)
    recipient.id = 7
    return recipient


@pytest.fixture
def session():
    return _Session(_recipient(), assignments=3, messages=48)


@pytest.fixture
def client(session, monkeypatch):
    app = create_app()

    async def _session():
        yield session

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[current_user] = lambda: _FakeAdmin()
    app.dependency_overrides[require_admin] = lambda: _FakeAdmin()
    app.dependency_overrides[dashboard_user] = lambda: _FakeAdmin()

    async def _record(_session, **kwargs):
        session.audits.append(kwargs)

    monkeypatch.setattr("app.api.v1.notifications.record_audit", _record)
    return TestClient(app, raise_server_exceptions=False)


class TestTheDelete:
    def test_a_recipient_can_be_deleted(self, client):
        response = client.delete("/api/v1/recipients/7")
        assert response.status_code == 204, response.text

    def test_the_row_is_actually_removed(self, client, session):
        """Not another deactivation. Every other "stop telling them" path on
        this resource keeps the row, which is exactly why this one exists."""
        client.delete("/api/v1/recipients/7")
        assert session.deletes, "no DELETE was issued"
        assert "DELETE FROM recipients" in session.deletes[0]

    def test_it_commits(self, client, session):
        client.delete("/api/v1/recipients/7")
        assert session.committed is True

    def test_someone_who_is_not_there_is_a_404(self, client, session):
        session.recipient = None
        response = client.delete("/api/v1/recipients/7")
        assert response.status_code == 404
        assert not session.deletes


class TestTheAuditTrail:
    """After the row is gone, this entry is the only record they existed."""

    def test_an_entry_is_written(self, client, session):
        client.delete("/api/v1/recipients/7")
        assert len(session.audits) == 1

    def test_it_satisfies_record_audits_signature(self, client, session):
        """The cheap version of the check that was missing when the alert
        numbers endpoint shipped: the bug was a missing required argument, and
        every test passed because nothing ran the handler body."""
        client.delete("/api/v1/recipients/7")
        audit = session.audits[0]
        for required in ("user", "action", "entity", "entity_id"):
            assert required in audit, f"record_audit was called without {required!r}"
        assert audit["action"] == "delete"
        assert audit["entity"] == "recipient"

    def test_it_keeps_who_was_deleted(self, client, session):
        client.delete("/api/v1/recipients/7")
        before = session.audits[0]["before"]
        assert before["name"] == "Priya, front office"
        assert before["phone_e164"] == "+919876543210"

    def test_it_keeps_how_much_went_with_them(self, client, session):
        """Counted before the delete, or the numbers would all be zero: the
        rows are gone by the time anything could count them."""
        client.delete("/api/v1/recipients/7")
        after = session.audits[0]["after"]
        assert after == {"assignments_deleted": 3, "messages_deleted": 48}

    def test_the_entry_is_recorded_before_the_row_is_deleted(self, client, session):
        """Ordering, not decoration. Both happen in one transaction, and a
        snapshot taken afterwards would have nothing to read."""
        order = []
        original_execute = session.execute

        async def _execute(statement):
            order.append("delete")
            return await original_execute(statement)

        session.execute = _execute
        session.audits = _RecordingList(order)

        client.delete("/api/v1/recipients/7")
        assert order == ["audit", "delete"]


class _RecordingList(list):
    """A list that notes when the handler appended to it."""

    def __init__(self, order):
        super().__init__()
        self._order = order

    def append(self, item):
        self._order.append("audit")
        super().append(item)
