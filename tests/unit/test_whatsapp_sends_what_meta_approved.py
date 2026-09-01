"""What actually leaves this machine when a price moves and WhatsApp is on.

This is the only delivery path that costs money per message and that depends on
a third party agreeing with us about a template we cannot see from here. Every
way it fails is quiet from the operator's side: an expired token, a template
approved under a slightly different language code, and a parameter count that
drifted by one all look identical in the dashboard -- "not delivered" -- and all
three are classified permanent, so nothing retries and nothing asks for
attention.

So these tests pin the exact bytes of the request and the exact verdict for each
answer Meta can give. They need no credentials and no approved template: respx
stands in for the Graph API, which is the point -- the contract is checkable
long before the account exists.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.notifications.base import (
    WHATSAPP_TEMPLATE_PARAM_COUNT,
    ChangeLine,
    Destination,
    RenderedMessage,
)
from app.notifications.providers import whatsapp_cloud
from app.notifications.providers.whatsapp_cloud import (
    _MAX_PARAM_CHARS,
    _PERMANENT_CODES,
    WhatsAppCloudProvider,
)
from app.notifications.render import render_digest

WHEN = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
GRAPH_URL = "https://graph.facebook.com/v21.0/1234567890/messages"

TO = Destination(name="Achuthan", email=None, phone_e164="+919876543210")


def _settings(**overrides):
    base = dict(
        whatsapp_enabled=True,
        whatsapp_graph_version="v21.0",
        whatsapp_phone_number_id="1234567890",
        whatsapp_access_token=SecretStr("test-token"),
        whatsapp_template_name="price_change_alert",
        whatsapp_template_lang="en",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def configured(monkeypatch):
    """A deployment with WhatsApp switched on, and no real credentials.

    Patches the module's own ``get_settings`` reference rather than the real
    one, which is lru_cached at process level -- otherwise the configured and
    unconfigured cases could not both run in one session.
    """
    monkeypatch.setattr(whatsapp_cloud, "get_settings", lambda: _settings())
    return WhatsAppCloudProvider()


def _line(**overrides) -> ChangeLine:
    fields = dict(
        hotel_name="Cliff View Resort",
        room_name="Deluxe Room",
        old_price=Decimal("3000"),
        new_price=Decimal("2700"),
        delta=Decimal("-300"),
        delta_pct=Decimal("-10"),
        currency="INR",
        direction="decrease",
        check_in="2026-08-28",
        check_out="2026-08-29",
    )
    fields.update(overrides)
    return ChangeLine(**fields)


def _message(**overrides) -> RenderedMessage:
    """A real rendered digest, not a hand-built stub.

    Built through ``render_digest`` on purpose: if the renderer and the template
    ever disagree about the parameter list, it has to fail here rather than at
    the first real price move.
    """
    return render_digest("Cliff View Resort", [_line(**overrides)], when=WHEN)


def _sent_body(route) -> dict:
    return json.loads(route.calls[0].request.content)


def _sent_params(route) -> list[str]:
    body = _sent_body(route)
    return [p["text"] for p in body["template"]["components"][0]["parameters"]]


def _accepted() -> httpx.Response:
    return httpx.Response(200, json={"messages": [{"id": "wamid.HBgM123"}]})


def _meta_error(code: int, message: str = "") -> dict:
    return {"error": {"code": code, "message": message or f"Meta says {code}"}}


class TestWhatMetaReceives:
    @respx.mock
    def test_a_successful_send_returns_metas_message_id(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        result = configured.send(TO, _message())

        assert route.called
        assert result.ok is True
        # Kept because the delivery webhook matches on it later; without it a
        # 'delivered' callback can never be tied back to a notification row.
        assert result.provider_message_id == "wamid.HBgM123"

    @respx.mock
    def test_a_200_with_no_message_id_is_still_a_success(self, configured):
        """Meta accepted it. Inventing an id would be worse than having none."""
        respx.post(GRAPH_URL).mock(return_value=httpx.Response(200, json={}))

        result = configured.send(TO, _message())

        assert result.ok is True
        assert result.provider_message_id is None

    @respx.mock
    def test_it_posts_the_template_not_the_rendered_text(self, configured):
        """The constraint the whole file exists for.

        A business-initiated message may not carry free text, so the email body
        must never appear in the payload -- only the template name and its
        positional values.
        """
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())
        message = _message()

        configured.send(TO, message)

        body = _sent_body(route)
        assert body["messaging_product"] == "whatsapp"
        assert body["type"] == "template"
        assert body["template"]["name"] == "price_change_alert"
        assert body["template"]["language"] == {"code": "en"}
        assert message.text not in route.calls[0].request.content.decode()

    @respx.mock
    def test_the_number_is_sent_without_the_plus(self, configured):
        """E.164 is what we store and what the API rejects."""
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message())

        assert _sent_body(route)["to"] == "919876543210"

    @respx.mock
    def test_the_access_token_is_sent_unmasked(self, configured):
        """Guards a real footgun: f-stringing the SecretStr instead of calling
        get_secret_value() sends '**********' and earns a silent 401."""
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message())

        assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"

    @respx.mock
    def test_the_seven_parameters_go_in_the_approved_order(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message())

        values = _sent_params(route)
        assert len(values) == WHATSAPP_TEMPLATE_PARAM_COUNT
        # Swapping two of these sends the old price where the new one belongs,
        # and the message still arrives looking perfectly reasonable.
        assert values[0] == "Cliff View Resort"
        assert values[1] == "Deluxe Room"
        assert values[2] == "₹3,000"
        assert values[3] == "₹2,700"
        assert values[4] == "-₹300 (10.0%)"

    @respx.mock
    def test_a_sold_out_room_says_so_rather_than_showing_zero(self, configured):
        """'Sold out' and 'dropped to nothing' must never look alike."""
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(
            TO,
            _message(direction="became_unavailable", new_price=None, delta=None,
                     delta_pct=None),
        )

        assert _sent_params(route)[4] == "sold out"

    @respx.mock
    def test_a_room_coming_back_says_so_rather_than_a_zero_rupee_drop(self, configured):
        """A became_available row carries delta 0.00, not NULL.

        The price it returned at is compared against the price it left at, so
        an unchanged rate is a real zero. Testing the delta before the
        direction sent that through the price-move branch and announced a room
        returning to sale as "-₹0 (0.0%)", with "now available" sitting in an
        else nothing could reach. The email for the same change read
        "available again" -- the two channels disagreed and only the quiet one
        was wrong.
        """
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(
            TO,
            _message(
                direction="became_available",
                old_price=Decimal("11475"),
                new_price=Decimal("11475"),
                delta=Decimal("0.00"),
                delta_pct=Decimal("0.00"),
            ),
        )

        assert _sent_params(route)[4] == "now available"


class TestParameterHygiene:
    """Room names are scraped off other people's pages.

    Meta rejects a parameter containing a newline, a tab, four or more
    consecutive spaces, or nothing at all -- every one of them as 132005, which
    is permanent. None of that is under our control at the source, so it is
    cleaned here.
    """

    @respx.mock
    def test_a_newline_in_a_room_name_is_flattened(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message(room_name="Deluxe Room\nwith balcony"))

        assert _sent_params(route)[1] == "Deluxe Room with balcony"

    @respx.mock
    def test_a_run_of_spaces_is_collapsed(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message(room_name="Deluxe      Room"))

        assert _sent_params(route)[1] == "Deluxe Room"

    @respx.mock
    def test_an_empty_parameter_becomes_a_dash(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message(room_name="   "))

        assert _sent_params(route)[1] == "—"

    @respx.mock
    def test_an_absurd_room_name_is_truncated_rather_than_refused(self, configured):
        """A page that returns its whole body as a room name must cost one
        ugly message, not a permanently failing one."""
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message(room_name="x" * 2000))

        room = _sent_params(route)[1]
        assert len(room) <= _MAX_PARAM_CHARS
        assert room.endswith("…")

    @respx.mock
    def test_nothing_meta_rejects_survives_into_the_payload(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(TO, _message(room_name="A\tB\n\nC     D"))

        for value in _sent_params(route):
            assert value
            assert "\n" not in value
            assert "\t" not in value
            assert "    " not in value


class TestWhenItRefusesToSend:
    """The cases that must never reach the network.

    Each would be a message that is billed and cannot arrive.
    """

    @respx.mock
    def test_an_unconfigured_provider_makes_no_request(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp_cloud, "get_settings", lambda: _settings(whatsapp_enabled=False)
        )
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        result = WhatsAppCloudProvider().send(TO, _message())

        assert not route.called
        assert result.error_code == "not_configured"
        assert result.retryable is False

    @respx.mock
    def test_a_missing_access_token_counts_as_unconfigured(self, monkeypatch):
        monkeypatch.setattr(
            whatsapp_cloud, "get_settings", lambda: _settings(whatsapp_access_token=None)
        )
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        assert WhatsAppCloudProvider().send(TO, _message()).error_code == "not_configured"
        assert not route.called

    @respx.mock
    def test_a_recipient_with_no_number_is_refused_not_attempted(self, configured):
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        result = configured.send(
            Destination(name="No Phone", email="a@b.c", phone_e164=None), _message()
        )

        assert not route.called
        assert result.error_code == "no_phone"
        assert result.retryable is False

    @respx.mock
    def test_a_message_with_no_template_params_is_refused(self, configured):
        """The ops-alert defect, caught at the provider.

        A message that is not about a price change carries no template
        parameters. The old code sent the subject as a single parameter against
        a seven-variable template, which Meta answered with 132000 -- permanent,
        so it was billed, lost, and never retried. Refusing costs nothing and
        says which template and how many were expected.
        """
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())
        ops_alert = RenderedMessage(
            subject="Monitoring has gone quiet", text="details", html=None
        )

        result = configured.send(TO, ops_alert)

        assert not route.called
        assert result.error_code == "template_params"
        assert result.retryable is False
        assert "7 parameters, got 0" in result.error_detail

    @respx.mock
    def test_the_subject_is_never_used_as_a_stand_in_parameter(self, configured):
        """Regression for the deleted `template_params or [message.subject]`."""
        route = respx.post(GRAPH_URL).mock(return_value=_accepted())

        configured.send(
            TO, RenderedMessage(subject="Monitoring has gone quiet", text="x")
        )

        assert not route.called


class TestHowFailuresAreClassified:
    """`retryable` is the only thing between a blip and a lost alert, and
    between a code bug and three billed repeats of it."""

    @respx.mock
    def test_a_rate_limit_is_worth_another_attempt(self, configured):
        respx.post(GRAPH_URL).mock(
            return_value=httpx.Response(429, json=_meta_error(130_429))
        )
        assert configured.send(TO, _message()).retryable is True

    @respx.mock
    def test_a_meta_outage_is_worth_another_attempt(self, configured):
        respx.post(GRAPH_URL).mock(return_value=httpx.Response(503, json={}))
        assert configured.send(TO, _message()).retryable is True

    @respx.mock
    def test_a_permanent_code_inside_a_500_is_still_retried(self, configured):
        """Pins the precedence: the HTTP status wins over the body's code."""
        respx.post(GRAPH_URL).mock(
            return_value=httpx.Response(500, json=_meta_error(132_001))
        )
        assert configured.send(TO, _message()).retryable is True

    @pytest.mark.parametrize("code", sorted(_PERMANENT_CODES))
    @respx.mock
    def test_a_permanent_meta_code_is_never_retried(self, configured, code):
        """131026 not on WhatsApp, 131047 re-engagement, 132000 param count,
        132001 template missing, 132005 param format, 133010 number
        unregistered. Every one fails identically on the next attempt."""
        respx.post(GRAPH_URL).mock(
            return_value=httpx.Response(400, json=_meta_error(code))
        )

        result = configured.send(TO, _message())

        assert result.ok is False
        assert result.error_code == str(code)
        assert result.retryable is False

    @respx.mock
    def test_an_unrecognised_code_is_retried_rather_than_dropped(self, configured):
        """Deliberately optimistic. An uncatalogued code is more likely
        transient, and three wasted attempts is a cheaper mistake than silently
        dropping a real price alert."""
        respx.post(GRAPH_URL).mock(
            return_value=httpx.Response(400, json=_meta_error(999_999))
        )
        assert configured.send(TO, _message()).retryable is True

    @respx.mock
    def test_a_4xx_with_no_code_at_all_is_not_retried(self, configured):
        respx.post(GRAPH_URL).mock(return_value=httpx.Response(400, text="nope"))

        result = configured.send(TO, _message())

        assert result.error_code == "http_400"
        assert result.retryable is False

    @respx.mock
    def test_the_error_detail_carries_metas_own_words(self, configured):
        """It is what the Send test button shows an operator, and the only
        clue distinguishing a bad token from an unapproved template."""
        respx.post(GRAPH_URL).mock(
            return_value=httpx.Response(
                400, json=_meta_error(132_001, "Template name does not exist")
            )
        )

        result = configured.send(TO, _message())

        assert "Template name does not exist" in result.error_detail
        assert len(result.error_detail) <= 500

    @respx.mock
    def test_a_dropped_connection_is_worth_another_attempt(self, configured):
        respx.post(GRAPH_URL).mock(side_effect=httpx.ConnectError("refused"))

        result = configured.send(TO, _message())

        assert result.error_code == "network"
        assert result.retryable is True

    @respx.mock
    def test_a_read_timeout_is_worth_another_attempt(self, configured):
        respx.post(GRAPH_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        assert configured.send(TO, _message()).retryable is True
