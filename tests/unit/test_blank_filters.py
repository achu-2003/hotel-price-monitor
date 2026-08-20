"""Pressing Filter with nothing selected must not 422.

A browser GET form submits every control it owns, including the empty ones.
"All hotels" is ``<option value="">`` and a cleared date input sends ``""``, so
the form the page itself renders produces:

    /changes?hotel_id=&hours=24&date_from=&date_to=

An ``int | None`` annotation rejects that empty string, and the person who
pressed the button the page gave them gets a wall of validation JSON instead of
their unfiltered list. Every case below is a URL the shipped templates can
generate on their own.

The distinction that matters is 422 versus anything-else: these routes redirect
to login without a session, so a 303 means the parameters parsed. A blank must
parse; genuine rubbish must still be refused, or "be lenient" has quietly
become "accept anything".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import db_session
from app.dashboard.routes import dashboard_user
from app.main import create_app


class _EmptySession:
    async def execute(self, *_a, **_k):
        raise AssertionError("these tests never reach the database")

    async def scalar(self, *_a, **_k):
        return 0


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app()

    async def _empty_session():
        yield _EmptySession()

    app.dependency_overrides[db_session] = _empty_session
    app.dependency_overrides[dashboard_user] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


class TestTheFormsThePagesRender:
    """Exactly what the templates emit when nothing is chosen."""

    @pytest.mark.parametrize(
        "url",
        [
            # changes.html: "All hotels" + both date inputs left empty
            "/changes?hotel_id=&hours=24&date_from=&date_to=",
            "/changes?hotel_id=&hours=720&date_from=&date_to=",
            # a hotel chosen, dates still blank
            "/changes?hotel_id=1&hours=24&date_from=&date_to=",
            # matrix.html: both dates cleared, adults cleared
            "/matrix?check_in=&check_out=&adults=",
            "/matrix?check_in=&check_out=&adults=2",
        ],
    )
    def test_a_blank_filter_is_not_a_validation_error(self, client, url):
        assert client.get(url, follow_redirects=False).status_code != 422


class TestRubbishIsStillRefused:
    """Leniency about "" must not become leniency about everything."""

    @pytest.mark.parametrize(
        "url",
        [
            "/changes?hotel_id=notanumber",
            "/changes?hotel_id=1.5",
            "/matrix?adults=99",       # above the le=20 bound
            "/matrix?adults=0",        # below the ge=1 bound
            "/matrix?check_in=notadate",
        ],
    )
    def test_it_is_rejected(self, client, url):
        assert client.get(url, follow_redirects=False).status_code == 422


class TestBoundsSurviveTheCoercion:
    """The bound sits inside the optional; the blank-coercion sits outside.

    Written the obvious way round -- ``ge`` on an ``int | None`` -- the
    constraint is eventually handed the None the coercion just produced and
    raises ``'>=' not supported between instances of 'NoneType' and 'int'``.
    That is a 500, or a 422 blaming the caller for a comparison the framework
    could not make. These two tests fail on that arrangement.
    """

    def test_blank_passes_where_a_bound_exists(self, client):
        assert client.get("/matrix?adults=", follow_redirects=False).status_code != 422

    def test_the_bound_still_bites(self, client):
        assert client.get("/matrix?adults=21", follow_redirects=False).status_code == 422


class TestValuesAreNotSilentlyDiscarded:
    """A real filter value must survive the coercion untouched."""

    def test_a_chosen_hotel_is_not_turned_into_none(self):
        from app.dashboard.routes import _blank_as_none

        assert _blank_as_none("7") == "7"

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_only_whitespace_counts_as_absent(self, blank):
        from app.dashboard.routes import _blank_as_none

        assert _blank_as_none(blank) is None

    def test_a_zero_is_a_value_not_a_blank(self):
        """``0`` is falsy; a naive ``if not value`` would drop it."""
        from app.dashboard.routes import _blank_as_none

        assert _blank_as_none("0") == "0"
        assert _blank_as_none(0) == 0
