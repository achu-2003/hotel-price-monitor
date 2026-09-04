"""The Alerts page's save button, exercised through the real route.

Written after the endpoint shipped with a call to ``record_audit`` that omitted
its required ``entity_id``. Every unit test passed: they covered the dispatcher
and the schema, and nothing ever executed the handler body. The failure only
appeared as a 500 the first time somebody pressed Save.

So this file calls the route itself. The database is a stand-in -- the point is
the handler's own logic and the shape of the calls it makes, not SQL.
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


class _AlertNumberSession:
    """Holds recipients in a list and answers the handler's queries.

    Narrow by design: the handler makes two kinds of query -- "the current
    alert numbers" and "a recipient with this phone" -- and both are answered
    from the same list.
    """

    def __init__(self, recipients=None):
        self.recipients = list(recipients or [])
        self.committed = False
        self.audits = []

    async def scalars(self, _statement):
        rows = [
            r for r in self.recipients if r.alerts_all_hotels and r.is_active
        ]

        class _Scalars:
            def all(self_inner):
                return rows

        return _Scalars()

    async def scalar(self, statement):
        # The "find by phone" lookup. The compiled parameters carry the number.
        wanted = statement.compile().params.get("phone_e164_1")
        for r in self.recipients:
            if r.phone_e164 == wanted:
                return r
        return None

    def add(self, obj):
        obj.id = len(self.recipients) + 1
        self.recipients.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None

    async def get(self, *_a, **_k):
        return None

    async def execute(self, *_a, **_k):
        return None


@pytest.fixture
def session():
    return _AlertNumberSession()


@pytest.fixture
def client(session, monkeypatch):
    app = create_app()

    async def _session():
        yield session

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[current_user] = lambda: _FakeAdmin()
    app.dependency_overrides[require_admin] = lambda: _FakeAdmin()
    app.dependency_overrides[dashboard_user] = lambda: _FakeAdmin()

    # The audit trail writes through its own session helper; recorded rather
    # than executed, so a failure here is visibly about the audit call.
    async def _record(_session, **kwargs):
        session.audits.append(kwargs)

    monkeypatch.setattr("app.api.v1.notifications.record_audit", _record)
    return TestClient(app, raise_server_exceptions=False)


def test_saving_numbers_succeeds(client):
    """The regression: this returned 500 because record_audit lacked entity_id."""
    response = client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "+919876543210", "name": "Front office"}]},
    )
    assert response.status_code == 200, response.text


def test_the_audit_entry_is_well_formed(client, session):
    """Whatever the audit records, it must satisfy record_audit's signature.

    Asserting on the keyword names is the cheap version of the check that was
    missing: the original bug was a missing required argument, not a wrong value.
    """
    client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "+919876543210", "name": "Front office"}]},
    )

    assert len(session.audits) == 1
    audit = session.audits[0]
    for required in ("user", "action", "entity", "entity_id"):
        assert required in audit, f"record_audit was called without {required!r}"


def test_a_saved_number_follows_every_hotel_and_skips_the_throttle(client, session):
    """The two flags are what make it an alert number rather than a contact."""
    client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "+919876543210", "name": "Front office"}]},
    )

    saved = session.recipients[0]
    assert saved.alerts_all_hotels is True
    assert saved.bypass_throttle is True
    assert saved.is_active is True


def test_the_name_typed_on_the_page_is_what_is_stored(client, session):
    client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "+919876543210", "name": "Priya, front office"}]},
    )

    assert session.recipients[0].name == "Priya, front office"


def test_a_number_with_nobody_attached_to_it_is_refused(client, session):
    """It used to be named after its own digits, which reads as configured.

    Settings is now the only list of who gets told anything, and the first
    question asked of a row in it is whose number that is. A list of five
    unnamed numbers cannot be pruned by anyone who did not type it.
    """
    response = client.put(
        "/api/v1/alert-numbers", json={"numbers": [{"phone_e164": "+919876543210"}]}
    )

    assert response.status_code == 422
    assert session.recipients == []


def test_a_name_of_only_spaces_is_refused(client):
    """max_length counts characters; three spaces render as nothing."""
    response = client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "+919876543210", "name": "   "}]},
    )

    assert response.status_code == 422


def test_a_number_dropped_from_the_list_stops_but_survives(client, session):
    """Removal keeps the row, so its delivery history stays answerable."""
    existing = Recipient(
        name="Old", phone_e164="+919000000001",
        alerts_all_hotels=True, bypass_throttle=True, is_active=True,
    )
    existing.id = 1
    session.recipients.append(existing)

    response = client.put("/api/v1/alert-numbers", json={"numbers": []})

    assert response.status_code == 200
    assert existing in session.recipients          # not deleted
    assert existing.is_active is False             # but silenced
    assert existing.alerts_all_hotels is False


def test_more_than_five_numbers_is_refused(client):
    numbers = [{"phone_e164": f"+91987654321{i}", "name": f"Person {i}"} for i in range(6)]

    response = client.put("/api/v1/alert-numbers", json={"numbers": numbers})

    assert response.status_code == 422


def test_the_same_number_twice_is_refused(client):
    """It would be messaged twice for every single price change."""
    response = client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "+919876543210", "name": "Front office"},
                          {"phone_e164": "+919876543210", "name": "Manager"}]},
    )

    assert response.status_code == 422


def test_a_number_that_is_not_e164_is_refused(client):
    response = client.put(
        "/api/v1/alert-numbers",
        json={"numbers": [{"phone_e164": "9876543210", "name": "Front office"}]},
    )

    assert response.status_code == 422
