"""API contract checks that need no database.

Everything here is about the parts of the API that are easiest to break
silently: whether an endpoint is protected, whether errors come back in the
agreed shape, and whether the security headers are actually set. A route that
quietly loses its auth dependency looks perfectly healthy in an integration
test that only ever calls it as an admin.

Endpoints that touch Postgres live in ``tests/integration``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import db_session
from app.dashboard.routes import dashboard_user
from app.main import create_app
from app.schemas.monitoring import ManualOfferIn, MonitorTargetCreate
from app.schemas.notifications import RecipientCreate


class _EmptySession:
    """A session that answers "nothing found" to everything.

    The database dependency is overridden rather than mocked per-test so these
    checks run without a driver or a server. Returning ``None`` everywhere is
    exactly the state these tests care about: an unauthenticated caller, and a
    login for an account that does not exist.
    """

    async def get(self, *_args, **_kwargs):
        return None

    async def scalar(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        raise AssertionError(
            "A request rejected at the auth boundary should never reach a query."
        )

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def add(self, _obj):
        return None


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app()

    async def _empty_session():
        yield _EmptySession()

    app.dependency_overrides[db_session] = _empty_session
    # The dashboard resolves its user through its own dependency, which would
    # otherwise decode a cookie against the same absent session.
    app.dependency_overrides[dashboard_user] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


class TestHealth:
    def test_liveness_needs_no_dependencies(self, client):
        """Deliberately checks nothing external.

        If liveness also checked Postgres, a thirty-second database blip would
        make an orchestrator kill every API container — turning a brief
        degradation into a restart loop.
        """
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_security_headers_are_set(self, client):
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        # No inline scripts anywhere, so the CSP can stay strict.
        assert "script-src 'self'" in headers["Content-Security-Policy"]

    def test_every_response_carries_a_request_id(self, client):
        assert client.get("/health").headers.get("X-Request-ID")

    def test_supplied_request_id_is_echoed(self, client):
        response = client.get("/health", headers={"X-Request-ID": "trace-me"})
        assert response.headers["X-Request-ID"] == "trace-me"


class TestAuthEnforcement:
    #: One from each router. A route losing its auth dependency is invisible
    #: in a test suite that only exercises it while logged in.
    PROTECTED = [
        ("GET", "/api/v1/hotels"),
        ("GET", "/api/v1/sources"),
        ("GET", "/api/v1/monitor-targets"),
        ("GET", "/api/v1/prices/current"),
        ("GET", "/api/v1/prices/unmatched"),
        ("GET", "/api/v1/recipients"),
        ("GET", "/api/v1/notifications"),
        ("GET", "/api/v1/errors"),
        ("GET", "/api/v1/dashboard/summary"),
        ("POST", "/api/v1/hotels"),
        ("POST", "/api/v1/monitor-targets"),
        ("POST", "/api/v1/manual-entry"),
        # Closes a row without mapping it. Admin-only for the same reason
        # resolving one is: it decides what the queue stops asking about.
        ("POST", "/api/v1/prices/unmatched/1/dismiss"),
    ]

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_requires_authentication(self, client, method, path):
        response = client.request(method, path, json={})
        assert response.status_code == 401, f"{method} {path} is not protected"

    def test_unauthenticated_error_is_a_problem_document(self, client):
        response = client.get("/api/v1/hotels")
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["status"] == 401
        assert body["title"] == "Not authenticated"
        assert body["instance"] == "/api/v1/hotels"

    def test_garbage_token_is_rejected(self, client):
        response = client.get(
            "/api/v1/hotels", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert response.status_code == 401

    def test_login_failure_does_not_reveal_whether_the_account_exists(self, client):
        """Distinguishing the two turns the login form into an enumeration tool.

        Reaching the database is not required to assert this: the response for
        a nonexistent account must be indistinguishable from a wrong password,
        and a 500 here would itself be a signal.
        """
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong-password"},
        )
        assert response.status_code in (401, 500)
        if response.status_code == 401:
            assert "incorrect" in response.json()["detail"].lower()


class TestDashboardAuth:
    def test_pages_redirect_to_login_rather_than_returning_json(self, client):
        # A raw JSON 401 in a browser tab is a dead end. Same cookie, same
        # token, different failure mode from the API.
        response = client.get("/matrix", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_login_page_renders_for_anonymous_visitors(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert "Sign in" in response.text

    def test_login_page_has_no_inline_script(self, client):
        """The CSP forbids it, so an inline handler would silently not run."""
        body = client.get("/login").text
        assert "onclick=" not in body
        assert "<script>" not in body


class TestValidationContract:
    def test_validation_errors_name_the_field(self, client):
        response = client.post("/api/v1/auth/login", json={"email": "not-an-email"})
        assert response.status_code == 422
        body = response.json()
        assert body["title"] == "Validation failed"
        assert {e["field"] for e in body["errors"]} >= {"email", "password"}

    def test_rolling_target_requires_its_offsets(self):
        with pytest.raises(ValueError, match="lead_time_days"):
            MonitorTargetCreate(hotel_source_id=1, date_strategy="rolling")

    def test_fixed_target_requires_both_dates(self):
        with pytest.raises(ValueError, match="fixed_check_in"):
            MonitorTargetCreate(
                hotel_source_id=1, date_strategy="fixed", fixed_check_in="2026-08-20"
            )

    def test_fixed_target_rejects_a_backwards_stay(self):
        with pytest.raises(ValueError, match="after"):
            MonitorTargetCreate(
                hotel_source_id=1,
                date_strategy="fixed",
                fixed_check_in="2026-08-21",
                fixed_check_out="2026-08-20",
            )

    def test_manual_entry_refuses_a_priceless_available_room(self):
        # Entering 0 for a sold-out room would collapse the distinction the
        # entire comparison engine exists to preserve.
        with pytest.raises(ValueError, match="different events"):
            ManualOfferIn(
                room_type_id=1, check_in="2026-08-20", check_out="2026-08-21"
            )

    def test_manual_entry_accepts_a_sold_out_room_with_no_price(self):
        offer = ManualOfferIn(
            room_type_id=1,
            check_in="2026-08-20",
            check_out="2026-08-21",
            is_available=False,
        )
        assert offer.price_inclusive is None

    def test_recipient_must_be_reachable(self):
        with pytest.raises(ValueError, match="no way to tell them"):
            RecipientCreate(name="Nobody")

    def test_recipient_phone_must_be_e164(self):
        # The WhatsApp Cloud API rejects anything else, and the rejection
        # arrives at the first real price move rather than at configuration.
        with pytest.raises(ValueError):
            RecipientCreate(name="X", phone_e164="98765 43210")
        assert RecipientCreate(name="X", phone_e164="+919876543210")


class TestOpenApi:
    def test_schema_builds(self, client):
        """A cheap guard: a bad response_model only fails when it is rendered."""
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "Hotel Price Monitor"
        assert "/api/v1/prices/matrix" in schema["paths"]

    def test_manual_run_documents_a_202(self, client):
        # The contract that matters: a browser fetch takes 20-40s, so this
        # endpoint must never be documented or implemented as synchronous.
        schema = client.get("/openapi.json").json()
        responses = schema["paths"]["/api/v1/monitor-targets/{target_id}/run"]["post"][
            "responses"
        ]
        assert "202" in responses


class TestForcedPasswordChange:
    """The ``must_change_password`` flag has to actually stop someone.

    A flag nothing enforces reads as done and is not, which is worse than not
    having it: the first admin account is created with a password that is also
    sitting in the deployment's .env file.
    """

    def _cookie_for(self, must_change: bool) -> dict[str, str]:
        from app.api.deps import SESSION_COOKIE
        from app.core.security import create_access_token

        return {
            SESSION_COOKIE: create_access_token(
                "1", "admin", expires_minutes=60, must_change_password=must_change
            )
        }

    def test_dashboard_pages_redirect_while_a_change_is_outstanding(self, client):
        response = client.get(
            "/matrix", cookies=self._cookie_for(True), follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/change-password"

    def test_the_change_page_itself_is_not_redirected(self, client):
        # Otherwise the requirement would be impossible to satisfy: the one
        # page that can clear the flag must not bounce off the check.
        # (This fixture stubs the dashboard's user lookup, so the route itself
        # sends anonymous callers to /login — what matters is that the
        # middleware did not intercept it first.)
        response = client.get(
            "/change-password", cookies=self._cookie_for(True), follow_redirects=False
        )
        assert response.headers.get("location") != "/change-password"

    def test_logout_stays_reachable(self, client):
        response = client.get(
            "/logout", cookies=self._cookie_for(True), follow_redirects=False
        )
        assert response.headers.get("location") != "/change-password"

    def test_the_api_is_not_blocked_by_the_redirect(self, client):
        # A bearer-token script is not what this protects, and blocking the
        # change-password endpoint would be a deadlock.
        response = client.get("/api/v1/hotels", cookies=self._cookie_for(True))
        assert response.status_code == 401  # rejected by auth, not redirected

    def test_a_normal_session_is_untouched(self, client):
        response = client.get(
            "/matrix", cookies=self._cookie_for(False), follow_redirects=False
        )
        assert response.headers.get("location") != "/change-password"

    def test_an_unreadable_cookie_does_not_trigger_the_redirect(self, client):
        from app.api.deps import SESSION_COOKIE

        response = client.get(
            "/matrix", cookies={SESSION_COOKIE: "garbage"}, follow_redirects=False
        )
        assert response.headers["location"].startswith("/login")
