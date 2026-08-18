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
from app.notifications.base import Destination, RenderedMessage, SendResult

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

        params = message.template_params or [message.subject]
        url = (
            f"https://graph.facebook.com/{settings.whatsapp_graph_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": destination.phone_e164.lstrip("+"),
            "type": "template",
            "template": {
                "name": settings.whatsapp_template_name,
                "language": {"code": settings.whatsapp_template_lang},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": str(p)} for p in params],
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
