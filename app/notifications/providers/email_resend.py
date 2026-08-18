"""Email via the Resend HTTP API.

Preferred over raw SMTP in production for one reason: it returns a message id
that can be correlated with delivery, bounce and complaint webhooks. SMTP
tells you the relay accepted the message and nothing after that, which means a
recipient whose mailbox silently rejects everything looks identical to one who
reads every alert.

The free tier comfortably covers this system's volume; cost was never the
constraint here, noise was.
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.core.logging import get_logger
from app.notifications.base import Destination, RenderedMessage, SendResult

log = get_logger("notify.resend")

_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class ResendEmailProvider:
    channel = "email"
    provider_name = "resend"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.resend_api_key and settings.email_from)

    def send(self, destination: Destination, message: RenderedMessage) -> SendResult:
        settings = get_settings()
        if not destination.email:
            return SendResult(
                ok=False, error_code="no_address",
                error_detail="Recipient has no email address", retryable=False,
            )
        if not settings.resend_api_key:
            return SendResult(
                ok=False, error_code="not_configured",
                error_detail="RESEND_API_KEY is not set", retryable=False,
            )

        payload = {
            "from": f"{settings.email_from_name} <{settings.email_from}>",
            "to": [destination.email],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html

        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(
                    _ENDPOINT,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            return SendResult(
                ok=False, error_code="network", error_detail=str(exc)[:500], retryable=True
            )

        if response.status_code in (200, 201):
            data = _safe_json(response)
            log.info("email_sent", provider="resend")
            return SendResult(ok=True, provider_message_id=str(data.get("id") or "") or None)

        # 429 and 5xx are the provider's problem and will pass; 4xx is ours and
        # will not, so retrying it only burns quota.
        retryable = response.status_code == 429 or response.status_code >= 500
        return SendResult(
            ok=False,
            error_code=f"http_{response.status_code}",
            error_detail=response.text[:500],
            retryable=retryable,
        )


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
