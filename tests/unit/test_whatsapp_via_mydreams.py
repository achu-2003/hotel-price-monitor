"""What leaves this machine when the reseller carries the message.

The My Dreams path is thinner than Meta's and fails more quietly. It has no
delivery callback, so "accepted" is the last thing we ever learn; it reports
errors as prose rather than codes, so classification rests on wording; and it
splits the template variables on commas, which the message content is full of.

The comma is the one that would have shipped. ``render.money`` writes Indian
rates as ₹1,23,456, so the FIRST real alert above a thousand rupees puts a
comma inside a parameter, the reseller splits there, and every later variable
lands one slot to the left -- a true number under a false label, with no error
raised anywhere. These tests pin that behaviour with realistic rates rather
than with a contrived room name, because that is how it would actually happen.

respx stands in for the reseller, so none of this needs the licence or an
approved template.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

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
from app.notifications.providers import whatsapp_mydreams
from app.notifications.providers.whatsapp_mydreams import (
    _MAX_PARAM_CHARS,
    MyDreamsWhatsAppProvider,
    _param_safe,
    _scrub,
)
from app.notifications.render import render_digest

WHEN = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SEND_URL = "https://wa.mydreamstechnology.in/api/sendtemplate.php"

TO = Destination(name="Achuthan", email=None, phone_e164="+919876543210")

API_KEY = "TzEtLuRZhaX4fK7yBJ6QSAw8v"


def _settings(**overrides):
    base = dict(
        whatsapp_enabled=True,
        whatsapp_provider="mydreams",
        whatsapp_template_name="price_change_alert",
        mydreams_base_url="https://wa.mydreamstechnology.in/api",
        mydreams_license_number="28825309344",
        mydreams_api_key=SecretStr(API_KEY),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def configured(monkeypatch):
    """WhatsApp on, routed through the reseller, with no real credentials.

    Patches the module's own ``get_settings`` reference, not the real one,
    which is lru_cached for the life of the process.
    """
    monkeypatch.setattr(whatsapp_mydreams, "get_settings", lambda: _settings())
    return MyDreamsWhatsAppProvider()


def _message(params=None):
    return RenderedMessage(
        subject="Price change",
        text="Price change",
        template_params=params or [str(i) for i in range(WHATSAPP_TEMPLATE_PARAM_COUNT)],
    )


def _sent_query(route):
    """The query string of the single request the provider made."""
    assert route.call_count == 1
    url = str(route.calls[0].request.url)
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ── the comma ────────────────────────────────────────────────────────


def test_an_ordinary_indian_rate_does_not_split_into_extra_parameters():
    """₹12,000 must not become two variables.

    This is the whole reason ``_param_safe`` exists. Seven variables must stay
    seven fields after the reseller splits on commas, for prices in the range
    every real hotel quotes.
    """
    params = [
        "Taj Palace",
        "Deluxe Room",
        "₹12,000",
        "₹13,500",
        "+₹1,500 (12.5%)",
        "20 Aug – 21 Aug",
        "10:30",
    ]
    joined = ",".join(_param_safe(p) for p in params)

    assert len(joined.split(",")) == WHATSAPP_TEMPLATE_PARAM_COUNT
    # And the values are still the right ones, in the right order.
    assert joined.split(",")[2] == "₹12000"
    assert joined.split(",")[3] == "₹13500"


def test_the_real_renderer_produces_parameters_that_survive_the_split(configured):
    """End to end, with the parameters ``render`` actually builds.

    The unit above uses hand-written strings; this one proves the same holds
    for whatever ``_whatsapp_params`` really emits, so the guarantee cannot be
    broken by a change to the renderer alone.
    """
    line = ChangeLine(
        hotel_name="Taj Palace",
        room_name="Deluxe Room",
        old_price=Decimal("112000"),      # ₹1,12,000 -- two grouping commas
        new_price=Decimal("123456.50"),
        delta=Decimal("11456.50"),
        delta_pct=Decimal("10.23"),
        currency="INR",
        direction="increase",
        check_in="2026-08-20",
        check_out="2026-08-21",
    )
    message = render_digest("Taj Palace", [line], when=WHEN)

    with respx.mock:
        route = respx.get(SEND_URL).mock(
            return_value=httpx.Response(200, text="Success")
        )
        result = configured.send(TO, message)

    assert result.ok
    assert len(_sent_query(route)["Param"].split(",")) == WHATSAPP_TEMPLATE_PARAM_COUNT


def test_a_comma_in_prose_becomes_a_semicolon_not_a_deletion():
    """A room name's comma is a list separator and must stay readable.

    Only the digit-grouping case is dropped outright; 'Deluxe, Sea View' would
    read as one run-on word if it were treated the same way.
    """
    assert _param_safe("Deluxe Room, Sea View") == "Deluxe Room; Sea View"


def test_url_encoding_would_not_have_saved_us():
    """Pins the reasoning, so nobody 'simplifies' this back to quote().

    The reseller decodes before it splits, so %2C round-trips to a comma and
    the field still breaks. The guarantee has to be that no comma is present at
    all -- httpx will percent-encode on the way out, and that is fine precisely
    because there is nothing left to encode.
    """
    assert "," not in _param_safe("₹12,000")
    assert "," not in _param_safe("a,b,c,d")


def test_newlines_and_blank_values_are_handled_like_the_meta_path():
    assert _param_safe("Deluxe\n\tRoom   Sea") == "Deluxe Room Sea"
    assert _param_safe("") == "—"
    assert _param_safe("   ") == "—"


def test_a_long_room_name_is_truncated_to_the_parameter_ceiling():
    assert len(_param_safe("x" * 5000)) == _MAX_PARAM_CHARS


# ── the request ──────────────────────────────────────────────────────


def test_the_request_carries_the_licence_template_and_bare_number(configured):
    with respx.mock:
        route = respx.get(SEND_URL).mock(
            return_value=httpx.Response(200, text="Success")
        )
        assert configured.send(TO, _message()).ok

    query = _sent_query(route)
    assert query["LicenseNumber"] == "28825309344"
    assert query["APIKey"] == API_KEY
    # The reseller documents 91XXXXXXXXXX -- E.164's '+' is not accepted.
    assert query["Contact"] == "919876543210"
    assert query["Template"] == "price_change_alert"


def test_a_wrong_parameter_count_is_refused_before_the_network(configured):
    """Costs nothing to refuse, and says why.

    Reachable whenever a notification rebuilds with no price_change rows behind
    it. Sending it anyway buys a message that lands wrong.
    """
    with respx.mock:
        route = respx.get(SEND_URL).mock(return_value=httpx.Response(200, text="Success"))
        result = configured.send(TO, _message(params=["only", "three", "here"]))

    assert route.call_count == 0
    assert not result.ok
    assert result.error_code == "template_params"
    assert not result.retryable


def test_a_recipient_without_a_number_is_refused(configured):
    result = configured.send(Destination(name="Nobody"), _message())
    assert not result.ok
    assert result.error_code == "no_phone"
    assert not result.retryable


def test_missing_credentials_report_as_unconfigured(monkeypatch):
    monkeypatch.setattr(
        whatsapp_mydreams, "get_settings", lambda: _settings(mydreams_api_key=None)
    )
    provider = MyDreamsWhatsAppProvider()
    assert not provider.is_configured()
    assert provider.send(TO, _message()).error_code == "not_configured"


# ── the answer ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    ["Success", "Message sent successfully", '{"status":"success","messageid":"abc123"}'],
)
def test_the_documented_shapes_of_success_are_accepted(configured, body):
    with respx.mock:
        respx.get(SEND_URL).mock(return_value=httpx.Response(200, text=body))
        assert configured.send(TO, _message()).ok


def test_a_message_id_is_kept_when_one_is_offered(configured):
    with respx.mock:
        respx.get(SEND_URL).mock(
            return_value=httpx.Response(
                200, json={"status": "success", "messageid": "wamid.XYZ"}
            )
        )
        assert configured.send(TO, _message()).provider_message_id == "wamid.XYZ"


@pytest.mark.parametrize(
    "body",
    [
        "Invalid APIKey",
        "Account Expired",
        "Template not found",
        "Invalid Contact number",
    ],
)
def test_a_permanent_refusal_is_not_retried(configured, body):
    """These fail identically forever; retrying only spends the quota."""
    with respx.mock:
        respx.get(SEND_URL).mock(return_value=httpx.Response(200, text=body))
        result = configured.send(TO, _message())

    assert not result.ok
    assert not result.retryable


def test_a_permanent_marker_outranks_the_word_sent(configured):
    """'Template not found, message not sent' contains a success keyword.

    Keyword matching in the other order would call this a delivered alert.
    """
    with respx.mock:
        respx.get(SEND_URL).mock(
            return_value=httpx.Response(200, text="Template not found, message not sent")
        )
        result = configured.send(TO, _message())

    assert not result.ok
    assert not result.retryable


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
def test_a_transient_http_failure_is_retried(configured, status_code):
    with respx.mock:
        respx.get(SEND_URL).mock(return_value=httpx.Response(status_code, text="busy"))
        result = configured.send(TO, _message())

    assert not result.ok
    assert result.retryable


def test_a_network_error_is_retried(configured):
    with respx.mock:
        respx.get(SEND_URL).mock(side_effect=httpx.ConnectError("connection refused"))
        result = configured.send(TO, _message())

    assert not result.ok
    assert result.error_code == "network"
    assert result.retryable


def test_an_unrecognisable_body_fails_loudly_and_only_once(configured):
    """The honest verdict when we genuinely cannot tell.

    Retrying might send a real person the same alert repeatedly and assuming
    success would lose it in silence, so this lands in the notification list
    for a human, and stays there.
    """
    with respx.mock:
        respx.get(SEND_URL).mock(return_value=httpx.Response(200, text="<html>maintenance"))
        result = configured.send(TO, _message())

    assert not result.ok
    assert result.error_code == "unrecognised_response"
    assert not result.retryable


# ── the credentials ──────────────────────────────────────────────────


def test_the_api_key_never_reaches_an_error_detail(configured):
    """error_detail is stored on the notification row and shown in the UI.

    httpx puts the full request URL in its exception messages, and with this
    reseller the URL is where the API key is.
    """
    with respx.mock:
        respx.get(SEND_URL).mock(
            side_effect=httpx.ConnectError(
                f"failed connecting to {SEND_URL}?LicenseNumber=28825309344&APIKey={API_KEY}"
            )
        )
        result = configured.send(TO, _message())

    assert API_KEY not in (result.error_detail or "")
    assert "28825309344" not in (result.error_detail or "")


def test_scrub_leaves_the_rest_of_the_message_readable():
    scrubbed = _scrub(f"GET /api/sendtemplate.php?LicenseNumber=288&APIKey={API_KEY} failed")
    assert API_KEY not in scrubbed
    assert "sendtemplate.php" in scrubbed
    assert "failed" in scrubbed
