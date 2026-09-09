"""WhatsApp via the Meta Cloud API.

THE CONSTRAINT THAT SHAPES THIS FILE
====================================
A message the business initiates — which every price alert is — must use a
**template Meta has pre-approved**, in the *utility* category. Free text is
only permitted inside a 24-hour window opened by the recipient writing to us
first, which never happens here.

So this provider does not send the rendered text at all. It sends the template
name plus positional parameters, and the wording lives in Meta's console. The
parameter order is fixed by the approved template and is produced by
``render._whatsapp_params``; changing one without the other silently sends the
old price where the new one should be.

**Template approval takes hours to days. Submit it on day one** — it is the
long pole in this phase, not the code.

Utility messages to India cost roughly ₹0.115 each. With digest batching the
realistic bill is a few hundred rupees a month, which is why the throttling in
``tasks_notify`` is about attention rather than money.
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.core.logging import get_logger
from app.notifications.base import (
    Destination,
    RenderedMessage,
    SendResult,
    whatsapp_template_for,
)

log = get_logger("notify.whatsapp")

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)

#: Meta error codes that will fail identically on every retry.
_PERMANENT_CODES = {
    131_026,  # recipient cannot receive messages (not on WhatsApp)
    131_047,  # re-engagement required; template rejected for this window
    132_000,  # template parameter count mismatch — a code bug, not a blip
    132_001,  # template does not exist / not approved
    132_005,  # template parameter format mismatch
    133_010,  # phone number not registered
}


class WhatsAppCloudProvider:
    channel = "whatsapp"
    provider_name = "meta_cloud"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.whatsapp_enabled
            and settings.whatsapp_phone_number_id
            and settings.whatsapp_access_token
        )

    def send(self, destination: Destination, message: RenderedMessage) -> SendResult:
        settings = get_settings()

        if not self.is_configured():
            return SendResult(
                ok=False, error_code="not_configured",
                error_detail="WhatsApp is not enabled or is missing credentials",
                retryable=False,
            )
        if not destination.phone_e164:
            return SendResult(
                ok=False, error_code="no_phone",
                error_detail="Recipient has no E.164 phone number", retryable=False,
            )

        # Which approved template carries THIS message, and how many variables
        # it was approved with. A price move and a market comparison are two
        # templates; see ``base.whatsapp_template_for``.
        template, expected = whatsapp_template_for(message.kind, settings)
        if not template:
            # The comparison template has not been approved on this deployment
            # yet. Refused rather than sent through the other one: a comparison
            # in the price-change template's slots is accepted by Meta, paid
            # for, and delivered reading like an alert about a room that never
            # moved.
            log.warning("whatsapp_template_not_configured", kind=message.kind)
            return SendResult(
                ok=False,
                error_code="template_not_configured",
                error_detail=(
                    f"No approved WhatsApp template is configured for "
                    f"{message.kind!r} messages"
                ),
                retryable=False,
            )

        # Deliberately no fallback to a single-parameter send. The approved
        # template has a fixed number of body variables, so any other count is
        # rejected with 132000 -- which is permanent, so the message is paid
        # for, lost, and never retried. Refusing here costs nothing and says
        # why.
        #
        # This is reachable: a notification whose price_change rows were
        # deleted rebuilds with no template params at all, and so does any
        # message that is not about a price change.
        params = message.template_params or []
        if len(params) != expected:
            log.error(
                "whatsapp_param_count_mismatch",
                template=template,
                expected=expected,
                got=len(params),
            )
            return SendResult(
                ok=False,
                error_code="template_params",
                error_detail=(
                    f"Template {template!r} takes {expected} parameters, "
                    f"got {len(params)}"
                ),
                retryable=False,
            )

        url = (
            f"https://graph.facebook.com/{settings.whatsapp_graph_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": destination.phone_e164.lstrip("+"),
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": settings.whatsapp_template_lang},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": _clean(p)} for p in params],
                    }
                ],
            },
        }

        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": (
                            f"Bearer {settings.whatsapp_access_token.get_secret_value()}"
                        ),
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            return SendResult(
                ok=False, error_code="network", error_detail=str(exc)[:500], retryable=True
            )

        data = _safe_json(response)

        if response.status_code == 200:
            message_id = _first_message_id(data)
            # 'sent' here means Meta accepted it. Actual delivery arrives later
            # on the status webhook, which is why the notification lifecycle
            # has separate sent/delivered/read states.
            log.info("whatsapp_accepted", message_id=message_id)
            return SendResult(ok=True, provider_message_id=message_id)

        error = (data.get("error") or {}) if isinstance(data, dict) else {}
        code = error.get("code")
        retryable = (
            response.status_code == 429
            or response.status_code >= 500
            or (code is not None and int(code) not in _PERMANENT_CODES)
        )
        return SendResult(
            ok=False,
            error_code=str(code or f"http_{response.status_code}"),
            error_detail=str(error.get("message") or response.text)[:500],
            retryable=retryable,
        )


#: Meta's ceiling on one body parameter. Generous for a price or a date range.
#: The one that can realistically reach it is a room name, which is scraped off
#: somebody else's page and is therefore untrusted in length as well as content.
_MAX_PARAM_CHARS = 700


def _clean(value: object) -> str:
    """Single-spaced within a line, never empty, bounded — NEWLINES KEPT.

    Meta's documentation rejects a tab, four or more consecutive spaces, and an
    empty value, all as 132005, which is permanent. Room names come from other
    people's markup, so any of those can arrive without warning, and collapsing
    beats refusing: a flattened name still says what moved, a dropped alert
    says nothing.

    THE NEWLINE IS NO LONGER ONE OF THEM
    ====================================
    The same documentation lists it, and this flattened it for that reason
    until the claim was actually tested: a parameter carrying "\n" was accepted
    on 9 Sep 2026 and arrived on the handset broken across the lines it asked
    for. ``render`` now uses that to lay a slot out as a block, so flattening
    here would quietly undo the layout of every summary.

    Scraped text is flattened by ``render._flat`` instead, at the point where a
    newline that means something can still be told apart from one that came out
    of somebody's <br>.
    """
    text = "\n".join(" ".join(part.split()) for part in str(value).splitlines())
    text = text.strip() or "—"
    if len(text) <= _MAX_PARAM_CHARS:
        return text
    return text[: _MAX_PARAM_CHARS - 1] + "…"


def _first_message_id(data: dict) -> str | None:
    try:
        return data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
