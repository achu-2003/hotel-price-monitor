"""GET /api/v1/hotels/{id} answered 500 for every hotel.

``HotelDetail`` declares ``room_types`` and ``recipients``. Validating the ORM
row straight into it read those two names off the ``Hotel`` object, which
resolves them as relationships -- a lazy load, and under the async session a
lazy load is ``MissingGreenlet``. The lists were queried explicitly two lines
earlier and attached afterwards; the validation step simply asked the row for
them first.

The dashboard never noticed: its buttons PATCH and DELETE that URL and never
GET it. Anything else on the API -- a script, a curl, the docs page -- got
"Internal server error" on every hotel.

Here the hotel is an object whose relationship attributes refuse to be read,
the way a detached async row does. Only a detail built from the columns can
survive it.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.deps import current_user, db_session
from app.main import create_app


class _Relationship:
    def __get__(self, obj, objtype=None):
        raise RuntimeError(
            "MissingGreenlet: reading a relationship off an async row is a lazy load"
        )


class _Hotel:
    """The columns of a Hotel, and two relationships that must not be touched."""

    id = 7
    name = "Treebo SNS Garden Inn"
    slug = "treebo-sns-garden-inn"
    location = "Yelagiri"
    latitude = None
    longitude = None
    is_own_property = False
    notes = None
    is_active = True
    created_at = datetime(2026, 9, 1, tzinfo=UTC)
    updated_at = datetime(2026, 9, 1, tzinfo=UTC)
    owner_user_id = 2

    room_types = _Relationship()
    recipients = _Relationship()


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return self._rows


class _Session:
    """Finds the hotel, and nothing attached to it."""

    async def get(self, _model, _id):
        return _Hotel()

    async def execute(self, *_a, **_k):
        return _Result()

    async def scalars(self, *_a, **_k):
        return _Result()

    async def scalar(self, *_a, **_k):
        return 0


class _User:
    id = 2


@pytest.fixture
def client() -> TestClient:
    app = create_app()

    async def _session():
        yield _Session()

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[current_user] = lambda: _User()
    return TestClient(app, raise_server_exceptions=False)


def test_the_detail_is_built_from_the_columns_not_the_row(client):
    response = client.get("/api/v1/hotels/7")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Treebo SNS Garden Inn"
    assert body["sources"] == []
    assert body["room_types"] == []
    assert body["recipients"] == []
    assert body["health"]["targets_total"] == 0
