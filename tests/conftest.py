"""Settings the unit suite must not inherit from whoever is running it.

WHY THIS FILE EXISTS
====================
``get_settings`` reads the developer's ``.env``, so any behaviour keyed on a
setting is keyed on that file — and the suite's verdict starts depending on the
machine. It did: ``PUBLIC_BASE_URL`` was empty, ``_comparison_url`` returned
before touching the session, and 1638 tests passed. Setting it to a real address
to test the alert links made twenty of them fail instantly, on a fake session
with no ``scalar`` — code that had never once run under test while the suite
reported green.

That is the same shape of blind spot as a double that ignores a WHERE clause:
nothing fails, and the passing run is evidence about the wrong configuration.

So the value is pinned here, for every unit test, whatever is in ``.env``. A
test that wants the other case says so — see ``test_an_alert_links_to_the_page_it_is_about``
and the ``linked_settings`` fixture below — which also means the two cases are
named and visible rather than one of them being whatever the box happened to
have.
"""
from __future__ import annotations

import os

import pytest

from app.config import get_settings


@pytest.fixture(scope="session", autouse=True)
def _pin_public_base_url():
    """No public address, unless a test asks for one.

    Empty is the right default for the suite because it is the default for a
    deployment: a link is only added once somebody has published the app on a
    domain. Tests about the linked case set it explicitly.
    """
    before = os.environ.get("PUBLIC_BASE_URL")
    os.environ["PUBLIC_BASE_URL"] = ""
    get_settings.cache_clear()
    yield
    if before is None:
        os.environ.pop("PUBLIC_BASE_URL", None)
    else:
        os.environ["PUBLIC_BASE_URL"] = before
    get_settings.cache_clear()


@pytest.fixture
def linked_settings():
    """A deployment that HAS been published, for the duration of one test.

    Returns the base URL it set, so a test can assert against the address
    rather than restating it.
    """
    base = "https://monitor.test.invalid"
    os.environ["PUBLIC_BASE_URL"] = base
    get_settings.cache_clear()
    try:
        yield base
    finally:
        os.environ["PUBLIC_BASE_URL"] = ""
        get_settings.cache_clear()
