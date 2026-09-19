"""Alerts reach WhatsApp through Sales Daddy, and its receipts come back.

Sales Daddy replaced the My Dreams reseller. These pin what leaves this
machine -- a JSON POST with the key in a header and the variables as an array,
commas intact -- how its error codes decide a retry, and that its signed status
webhook moves a notification along without letting an unsigned caller do the
same.

respx stands in for Sales Daddy, so none of this needs a real key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import db_session
from app.dashboard.routes import dashboard_user
from app.db.models import NotificationStatus
from app.main import create_app
from app.notifications import registry
from app.notifications.base import (
    MARKET_COMPARISON,
    WHATSAPP_TEMPLATE_PARAM_COUNT,
    Destination,
    RenderedMessage,
)
from app.notifications.providers import whatsapp_salesdaddy
from app.notifications.providers.whatsapp_salesdaddy import SalesDaddyWhatsAppProvider

SEND_URL = "https://api.salesdaddy.in/v1/wa/send"
API_KEY = "sd_live_test_key"
TO = Destination(name="Achuthan", email=None, phone_e164="+919876543210")


def _settings(**overrides):
    base = dict(
        whatsapp_enabled=True,
        whatsapp_provider="salesdaddy",
        whatsapp_template_name="rate_update_alert",
        whatsapp_template_lang="en",
        whatsapp_comparison_template_name="market_comparison_alert_v2",
        whatsapp_comparison_template_params=5,
        salesdaddy_base_url="https://api.salesdaddy.in",
        salesdaddy_api_key=SecretStr(API_KEY),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(whatsapp_salesdaddy, "get_settings", lambda: _settings())
    return SalesDaddyWhatsAppProvider()


def _message(params=None, kind="price_change"):
    if params is None:
        params = ["MGM Whispering Nest", "Villa, 3 Bedroom", "₹12,000", "₹13,500",
                  "+₹1,500", "20 Sep → 21 Sep", "10:25 AM IST"]
    return RenderedMessage(subject="s", text="t", template_params=params, kind=kind)


class TestTheSend:
    @respx.mock
    def test_posts_the_template_as_json_with_the_key_in_a_header(self, provider):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                200, json={"ok": True, "messageId": "msg_7Q2x", "status": "queued"}
            )
        )

        result = provider.send(TO, _message())

        assert result.ok and result.provider_message_id == "msg_7Q2x"
        request = route.calls.last.request
        assert request.headers["X-Api-Key"] == API_KEY
        assert API_KEY not in str(request.url)
        body = json.loads(request.content)
        assert body["to"] == "919876543210"
        assert body["type"] == "template"
        assert body["template"] == "rate_update_alert"
        assert body["language"] == "en"
        assert body["name"] == "Achuthan"

    @respx.mock
    def test_commas_inside_a_value_survive(self, provider):
        """The reseller split on them; a JSON array has no reason to."""
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "messageId": "m"})
        )

        provider.send(TO, _message())

        params = json.loads(route.calls.last.request.content)["params"]
        assert len(params) == WHATSAPP_TEMPLATE_PARAM_COUNT
        assert params[1] == "Villa, 3 Bedroom"
        assert params[2] == "₹12,000"

    @respx.mock
    def test_a_summary_goes_through_the_comparison_template(self, provider):
        route = respx.post(SEND_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "messageId": "m"})
        )

        provider.send(TO, _message(["a", "b", "c", "d", "e"], kind=MARKET_COMPARISON))

        assert json.loads(route.calls.last.request.content)["template"] == (
            "market_comparison_alert_v2"
        )

    @respx.mock
    def test_a_wrong_parameter_count_is_refused_before_any_request(self, provider):
        route = respx.post(SEND_URL)

        result = provider.send(TO, _message(["only one"]))

        assert not result.ok and result.error_code == "template_params"
        assert not result.retryable
        assert not route.called

    def test_not_configured_without_a_key(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp_salesdaddy, "get_settings", lambda: _settings(salesdaddy_api_key=None)
        )
        assert not SalesDaddyWhatsAppProvider().is_configured()


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "code", "retryable"),
        [
            (401, "invalid_key", False),
            (409, "window_closed", False),
            (422, "template_not_approved", False),
            (422, "template_params_mismatch", False),
            (422, "invalid_number", False),
            (429, "rate_limited", True),
            (502, "whatsapp_error", True),
        ],
    )
    @respx.mock
    def test_the_code_decides_whether_to_retry(self, provider, status, code, retryable):
        respx.post(SEND_URL).mock(
            return_value=httpx.Response(
                status, json={"ok": False, "error": code, "message": "explained"}
            )
        )

        result = provider.send(TO, _message())

        assert not result.ok
        assert result.error_code == code
        assert result.retryable is retryable

    @respx.mock
    def test_a_network_failure_is_retried(self, provider):
        respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("boom"))

        result = provider.send(TO, _message())

        assert not result.ok and result.error_code == "network" and result.retryable


def test_the_registry_routes_whatsapp_to_salesdaddy(monkeypatch):
    monkeypatch.setattr(registry, "get_settings", lambda: _settings())
    registry.reset_cache()
    try:
        assert registry.get_provider("whatsapp").provider_name == "salesdaddy"
    finally:
        registry.reset_cache()


# -- the status webhook -----------------------------------------------
SECRET = "webhook-secret"


class _Notification:
    def __init__(self, message_id="msg_7Q2x", status=NotificationStatus.SENT):
        self.provider_message_id = message_id
        self.status = status
        self.delivered_at = None
        self.error_code = None
        self.error_detail = None


class _FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.commits = 0

    async def scalar(self, *_args, **_kwargs):
        return self.rows.pop(0) if self.rows else None

    async def commit(self):
        self.commits += 1


@pytest.fixture
def webhook(monkeypatch):
    from app.api.v1 import notifications as module

    def build(session, *, provider="salesdaddy", secret=SECRET, allow_unsigned=False):
        app = create_app()

        async def _session():
            yield session

        app.dependency_overrides[db_session] = _session
        app.dependency_overrides[dashboard_user] = lambda: None
        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: SimpleNamespace(
                whatsapp_provider=provider,
                salesdaddy_webhook_secret=SecretStr(secret) if secret else None,
                whatsapp_webhook_allow_unsigned=allow_unsigned,
            ),
        )
        return TestClient(app, raise_server_exceptions=False)

    return build


def _post(client, payload, *, secret=SECRET, prefix=""):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Signature-256"] = prefix + digest
    return client.post("/api/v1/webhooks/salesdaddy", content=raw, headers=headers)


class TestTheWebhook:
    @pytest.mark.parametrize("prefix", ["", "sha256="])
    def test_delivered_moves_the_notification_along(self, webhook, prefix):
        row = _Notification()
        client = webhook(_FakeSession([row]))

        response = _post(
            client,
            {"event": "message.status", "messageId": "msg_7Q2x", "status": "delivered"},
            prefix=prefix,
        )

        assert response.status_code == 200
        assert row.status is NotificationStatus.DELIVERED
        assert row.delivered_at is not None

    def test_failed_records_the_reason(self, webhook):
        row = _Notification()
        client = webhook(_FakeSession([row]))

        _post(client, {
            "event": "message.status", "messageId": "msg_7Q2x", "status": "failed",
            "error": "whatsapp_error", "reason": "Message undeliverable",
        })

        assert row.status is NotificationStatus.FAILED
        assert row.error_code == "whatsapp_error"
        assert row.error_detail == "Message undeliverable"

    def test_a_late_delivered_does_not_undo_a_failure(self, webhook):
        row = _Notification(status=NotificationStatus.FAILED)
        client = webhook(_FakeSession([row]))

        _post(client, {"event": "message.status", "messageId": "msg_7Q2x",
                       "status": "delivered"})

        assert row.status is NotificationStatus.FAILED

    def test_a_customer_reply_touches_nothing(self, webhook):
        session = _FakeSession([_Notification()])
        client = webhook(session)

        response = _post(client, {"event": "message.received", "from": "919876543210",
                                  "type": "text", "text": "STOP"})

        assert response.status_code == 200 and session.commits == 0

    def test_a_bad_signature_is_refused(self, webhook):
        row = _Notification()
        client = webhook(_FakeSession([row]))

        response = _post(
            client,
            {"event": "message.status", "messageId": "msg_7Q2x", "status": "failed"},
            secret="not-the-secret",
        )

        assert response.status_code == 403
        assert row.status is NotificationStatus.SENT

    def test_unsigned_is_refused_without_a_secret(self, webhook):
        client = webhook(_FakeSession([_Notification()]), secret=None)

        response = _post(client, {"event": "message.status", "messageId": "m",
                                  "status": "read"}, secret=None)

        assert response.status_code == 403

    def test_not_found_on_another_provider(self, webhook):
        client = webhook(_FakeSession(), provider="mydreams")

        response = _post(client, {"event": "message.status", "messageId": "m",
                                  "status": "read"})

        assert response.status_code == 404
