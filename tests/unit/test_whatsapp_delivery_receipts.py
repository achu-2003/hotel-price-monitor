"""The two endpoints Meta calls, which nothing else in the suite covered.

Both are unauthenticated by necessity -- Meta has no session with us -- so the
verify token and the request signature are the only things standing between
them and the open internet. The POST handler also writes delivery history,
which is the record an operator trusts when asking "did that alert actually
reach anyone?". A bug here does not break a send; it silently rewrites the
answer to that question.

The other rule this file pins is that the POST handler must answer 200 to
almost everything. Meta retries any other status with escalating frequency, so
a parse error on our side becomes a retry storm rather than a log line.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import db_session
from app.dashboard.routes import dashboard_user
from app.db.models import NotificationStatus
from app.main import create_app

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


class _Notification:
    """Just enough of the ORM row for the handler to act on."""

    def __init__(self, message_id="wamid.ABC", status=NotificationStatus.SENT):
        self.provider_message_id = message_id
        self.status = status
        self.delivered_at = None
        self.error_code = None
        self.error_detail = None


class _FakeSession:
    """Serves notifications in order, ignoring the WHERE clause.

    The same trade as ``tests/unit/test_email_delivery.FakeSession``: this says
    nothing about the SQL and everything about what the handler does with a row
    once it has one.
    """

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.commits = 0

    async def scalar(self, *_args, **_kwargs):
        return self.rows.pop(0) if self.rows else None

    async def commit(self):
        self.commits += 1


def _client(
    session, *, app_secret=None, verify_token=None,
    provider="meta_cloud", allow_unsigned=True,
) -> TestClient:
    from app.api.v1 import notifications as module

    app = create_app()

    async def _session():
        yield session

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[dashboard_user] = lambda: None

    class _Settings:
        whatsapp_app_secret = _Secret(app_secret) if app_secret else None
        whatsapp_webhook_verify_token = _Secret(verify_token) if verify_token else None
        # Only Meta sends these callbacks, so the endpoint 404s on any other
        # provider. Every test here is about Meta's payloads.
        whatsapp_provider = provider
        # These tests predate the signature becoming mandatory and are about
        # what the handler does with a payload, not about who may send one.
        # The refusal itself is covered in TestUnsignedCallbacks below.
        whatsapp_webhook_allow_unsigned = allow_unsigned

    module.get_settings = lambda: _Settings()
    return TestClient(app, raise_server_exceptions=False)


class _Secret:
    def __init__(self, value):
        self._value = value

    def get_secret_value(self):
        return self._value


@pytest.fixture(autouse=True)
def _restore_settings():
    """create_app is called per test, but get_settings is patched on the module
    object, so it has to be put back or later files inherit the stub."""
    from app.api.v1 import notifications as module
    from app.config import get_settings as real

    yield
    module.get_settings = real


def _post(client, payload, *, secret=None, signature=None):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    elif secret:
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return client.post("/api/v1/webhooks/whatsapp", content=raw, headers=headers)


def _status_payload(message_id="wamid.ABC", state="delivered", errors=None):
    record = {"id": message_id, "status": state}
    if errors is not None:
        record["errors"] = errors
    return {"entry": [{"changes": [{"value": {"statuses": [record]}}]}]}


class TestTheVerificationHandshake:
    def test_the_challenge_is_echoed_verbatim(self):
        """Meta subscribes only on the bare challenge string. Wrapping it in
        JSON fails the handshake with no error anyone ever sees."""
        client = _client(_FakeSession(), verify_token=VERIFY_TOKEN)

        response = client.get(
            "/api/v1/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "12345",
                "hub.verify_token": VERIFY_TOKEN,
            },
        )

        assert response.status_code == 200
        assert response.text == "12345"
        assert response.headers["content-type"].startswith("text/plain")

    def test_a_wrong_token_is_rejected(self):
        client = _client(_FakeSession(), verify_token=VERIFY_TOKEN)

        response = client.get(
            "/api/v1/webhooks/whatsapp",
            params={"hub.challenge": "12345", "hub.verify_token": "wrong"},
        )

        assert response.status_code == 403

    def test_an_unset_token_rejects_everything(self):
        """Load-bearing: hmac.compare_digest("", "") is True, so the explicit
        "is it configured at all" check is the only thing stopping a deployment
        that never set the token from having a wide-open endpoint."""
        client = _client(_FakeSession(), verify_token=None)

        response = client.get(
            "/api/v1/webhooks/whatsapp",
            params={"hub.challenge": "12345", "hub.verify_token": ""},
        )

        assert response.status_code == 403


class TestTheSignature:
    def test_a_correctly_signed_callback_is_accepted(self):
        session = _FakeSession([_Notification()])
        client = _client(session, app_secret=APP_SECRET)

        response = _post(client, _status_payload(), secret=APP_SECRET)

        assert response.status_code == 200
        assert response.json()["updated"] == 1

    def test_an_unsigned_callback_is_rejected_once_a_secret_exists(self):
        """Without this the endpoint takes an arbitrary provider_message_id
        from anyone who learns the URL, and delivery history -- including
        'failed' -- becomes writable by strangers."""
        session = _FakeSession([_Notification()])
        client = _client(session, app_secret=APP_SECRET)

        response = _post(client, _status_payload(), signature="sha256=deadbeef")

        assert response.status_code == 403
        assert session.commits == 0

    def test_it_still_accepts_callbacks_before_a_secret_is_configured(self):
        """So the webhook can be wired up on the day the number is created,
        before anyone has been to the app-settings page. Warned about on every
        call rather than silently permitted."""
        session = _FakeSession([_Notification()])
        client = _client(session, app_secret=None)

        response = _post(client, _status_payload())

        assert response.status_code == 200
        assert response.json()["updated"] == 1


class TestStatusCallbacks:
    def test_a_delivered_receipt_advances_the_notification(self):
        row = _Notification()
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        _post(client, _status_payload(state="delivered"))

        assert row.status is NotificationStatus.DELIVERED
        assert row.delivered_at is not None
        assert session.commits == 1

    def test_a_read_receipt_records_that_it_was_seen(self):
        row = _Notification()
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        _post(client, _status_payload(state="read"))

        assert row.status is NotificationStatus.READ

    def test_a_failure_records_metas_code_and_title(self):
        row = _Notification()
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        _post(
            client,
            _status_payload(
                state="failed",
                errors=[{"code": 131_026, "title": "Receiver incapable"}],
            ),
        )

        assert row.status is NotificationStatus.FAILED
        assert row.error_code == "131026"
        assert "Receiver" in row.error_detail

    def test_a_failure_with_no_error_block_still_records_something(self):
        row = _Notification()
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        _post(client, _status_payload(state="failed"))

        assert row.error_code == "unknown"

    def test_a_late_delivered_does_not_undo_a_read(self):
        """Meta does not guarantee callback order, and 'somebody opened it' is
        the most valuable thing this webhook reports -- it must not be
        overwritten by a receipt describing an earlier moment."""
        row = _Notification(status=NotificationStatus.READ)
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        response = _post(client, _status_payload(state="delivered"))

        assert row.status is NotificationStatus.READ
        assert response.json()["updated"] == 0
        assert session.commits == 0

    def test_a_late_delivered_does_not_undo_a_failure(self):
        row = _Notification(status=NotificationStatus.FAILED)
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        _post(client, _status_payload(state="delivered"))

        assert row.status is NotificationStatus.FAILED

    def test_a_sent_receipt_changes_nothing_and_commits_nothing(self):
        """We recorded 'sent' ourselves. Committing for it was a write per
        callback that moved no row."""
        row = _Notification()
        session = _FakeSession([row])
        client = _client(session, app_secret=None)

        response = _post(client, _status_payload(state="sent"))

        assert response.json()["updated"] == 0
        assert session.commits == 0


class TestWhatItRefusesToChokeOn:
    """Anything but a 200 here makes Meta retry with escalating frequency."""

    def test_a_status_for_a_message_we_never_sent_is_ignored(self):
        session = _FakeSession([])
        client = _client(session, app_secret=None)

        response = _post(client, _status_payload(message_id="wamid.UNKNOWN"))

        assert response.status_code == 200
        assert response.json()["updated"] == 0
        assert session.commits == 0

    def test_a_body_that_is_not_json_is_accepted_and_ignored(self):
        client = _client(_FakeSession(), app_secret=None)

        response = client.post(
            "/api/v1/webhooks/whatsapp",
            content=b"<html>an error page</html>",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

    def test_a_json_body_that_is_not_an_object_is_accepted_and_ignored(self):
        client = _client(_FakeSession(), app_secret=None)

        response = _post(client, ["not", "an", "object"])

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"

    def test_an_empty_entry_list_is_not_an_error(self):
        client = _client(_FakeSession(), app_secret=None)

        response = _post(client, {"entry": []})

        assert response.status_code == 200
        assert response.json()["updated"] == 0

    def test_a_status_with_no_message_id_is_skipped(self):
        client = _client(_FakeSession(), app_secret=None)

        response = _post(
            client, {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}
        )

        assert response.json()["updated"] == 0

    def test_several_statuses_in_one_callback_are_all_recorded(self):
        """Meta batches them."""
        rows = [_Notification("wamid.A"), _Notification("wamid.B")]
        session = _FakeSession(rows)
        client = _client(session, app_secret=None)

        response = _post(
            client,
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "statuses": [
                                        {"id": "wamid.A", "status": "delivered"},
                                        {"id": "wamid.B", "status": "read"},
                                    ]
                                }
                            }
                        ]
                    }
                ]
            },
        )

        assert response.json()["updated"] == 2
        assert rows[0].status is NotificationStatus.DELIVERED
        assert rows[1].status is NotificationStatus.READ
        assert session.commits == 1


# ── who is allowed to call this at all ───────────────────────────────
#
# WHAT WAS OPEN
# =============
# With WHATSAPP_APP_SECRET empty the POST endpoint accepted any unsigned body
# from anyone who could reach it, logged a warning, and carried on. Probed
# against a running instance it answered:
#
#     POST /api/v1/webhooks/whatsapp   (no signature, no session)
#     -> 200 {"status":"ok","updated":0}
#
# Only "updated: 0" because the probe used a message id matching nothing. A
# guessed provider_message_id would have marked a real notification delivered,
# read, or FAILED -- and failure is terminal, so a genuine callback arriving
# afterwards could not have put it right.
#
# The comment called unsigned a temporary convenience for the minutes before
# the app secret arrives. Nothing made it temporary, and it had quietly become
# the permanent state. It is now an explicit setting, off by default.
#
# None of it was reachable anyway: the deployment runs on the My Dreams
# reseller, which publishes no callback at all, so the endpoint could not
# receive a legitimate request. It was attack surface with no purpose.
class TestUnsignedCallbacks:
    def test_an_unsigned_callback_is_refused_by_default(self):
        client = _client(_FakeSession(), allow_unsigned=False)

        response = _post(client, _status_payload("wamid.ABC", "delivered"))

        assert response.status_code == 403

    def test_the_refusal_names_both_ways_out_of_it(self):
        client = _client(_FakeSession(), allow_unsigned=False)

        detail = _post(client, _status_payload()).json()["detail"]

        assert "WHATSAPP_APP_SECRET" in detail
        assert "WHATSAPP_WEBHOOK_ALLOW_UNSIGNED" in detail

    def test_a_refused_callback_changes_nothing(self):
        row = _Notification()
        client = _client(_FakeSession([row]), allow_unsigned=False)

        _post(client, _status_payload(row.provider_message_id, "failed"))

        assert row.status is not NotificationStatus.FAILED

    def test_the_escape_hatch_still_works_when_asked_for(self):
        """Explicit and visible, for the minutes before the secret arrives."""
        client = _client(_FakeSession(), allow_unsigned=True)

        response = _post(client, _status_payload())

        assert response.status_code == 200

    def test_a_signed_callback_never_needs_the_escape_hatch(self):
        client = _client(_FakeSession(), app_secret="s3cret", allow_unsigned=False)

        response = _post(client, _status_payload(), secret="s3cret")

        assert response.status_code == 200


class TestTheEndpointIsClosedOnOtherProviders:
    """Only Meta sends these. On the reseller it is surface with no purpose."""

    def test_the_post_endpoint_is_not_found_on_mydreams(self):
        client = _client(_FakeSession(), provider="mydreams", app_secret="s3cret")

        response = _post(client, _status_payload(), secret="s3cret")

        assert response.status_code == 404

    def test_the_verification_handshake_is_not_found_either(self):
        client = _client(_FakeSession(), provider="mydreams", verify_token=VERIFY_TOKEN)

        response = client.get(
            "/api/v1/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.challenge": "12345",
                    "hub.verify_token": VERIFY_TOKEN},
        )

        assert response.status_code == 404

    def test_a_correctly_signed_callback_is_still_refused(self):
        """Closed means closed: a valid signature does not reopen it.

        The reseller holds no signing secret, so anything arriving here with a
        valid one is not the reseller.
        """
        row = _Notification()
        client = _client(_FakeSession([row]), provider="mydreams", app_secret="s3cret")

        _post(client, _status_payload(row.provider_message_id, "failed"), secret="s3cret")

        assert row.status is not NotificationStatus.FAILED
