"""The alert settings the unit suite must not read from whoever is running it.

``monitoring.stored_defaults`` reads the ``alert_defaults`` row through a real
``sync_session`` -- the developer's own database, not the fake each test hands
the code under test. On 25 Sep that row had both rate channels switched off on
Settings, and 31 notification tests failed at once: every dispatch correctly
sent nothing, on a machine whose operator had asked for nothing. The same row
switched back on would have passed them, so the verdict was a fact about the
box, not the code -- the blind spot ``tests/conftest.py`` describes for
``PUBLIC_BASE_URL``.

So every unit test gets the fallback a deployment gets before anyone has
touched Settings, taken from ``stored_defaults`` itself with the database read
made to fail -- which is the path that fallback exists for -- rather than
restated here, where it could drift from the real one.
"""
from __future__ import annotations

import math

import pytest

import app.db.session as db_session
from app.services import monitoring


@pytest.fixture(autouse=True)
def _pin_stored_defaults(monkeypatch):
    def unreadable():
        raise RuntimeError("unit tests do not read the developer's alert_defaults")

    monitoring.forget_stored_defaults()
    with monkeypatch.context() as during_read:
        during_read.setattr(db_session, "sync_session", unreadable)
        defaults = monitoring.stored_defaults()
    monkeypatch.setattr(monitoring, "_cached_defaults", (math.inf, defaults))
    yield
    monitoring.forget_stored_defaults()
