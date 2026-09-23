"""WhatsApp via Sales Daddy, which replaced My Dreams Technology in Sep 2026.

HOW THIS DIFFERS FROM ``whatsapp_mydreams``
==========================================
Everything the reseller made awkward is gone:

  * the send is a JSON **POST** to ``/v1/wa/send``;
  * the key travels in the ``X-Api-Key`` header, never in a URL, so there is
    nothing to scrub out of logs;
  * template variables are a JSON **array**, so a comma inside a value is just
    a comma -- Indian digit grouping survives, and nothing has to fit a query
    string;
  * errors come back as stable codes (``template_not_approved``,
    ``window_closed``...), not prose;
  * delivery is reported, either by polling ``/v1/wa/messages/{id}`` or on a
    signed webhook -- see ``api.v1.notifications.salesdaddy_status_webhook``.

As on every path, a business-initiated message must be a Meta-approved
template, so this sends the template name plus positional parameters and never
the rendered text. The template has to be approved in Sales Daddy
(Inbox -> Templates); an approval on the old reseller does not carry over.

The key is tied to one company and one WhatsApp number, so there is no
phone-number id to configure: whatever number the key was issued for is the
number the alerts come from.
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
from app.notifications.providers.whatsapp_cloud import _clean

#: What a newline becomes in a parameter on THIS route.
#:
#: A middle dot, not a space: the slots that carry newlines are laid out as
#: blocks -- a property name with its rooms under it, or the stamp with the
#: comparison link after it -- and flattening those on a space runs the heading
#: into the first room. The separator is the one the stamp already uses, so a
#: flattened block reads as a list rather than as a sentence that lost its
#: punctuation.
_NEWLINE_AS = " · "

log = get_logger("notify.whatsapp.salesdaddy")

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)

#: Error codes from the API guide (section 12) that will fail identically on
#: every retry. Anything else -- ``rate_limited``, ``whatsapp_error``, a 5xx,
#: a code the guide does not list yet -- is worth another attempt.
_PERMANENT_ERRORS = {
    "invalid_key",
    "api_access_disabled",
    "number_not_connected",
    "window_closed",
    "template_not_approved",
    "template_params_mismatch",
    "invalid_number",
    "invalid_request",
}


def _one_line(text: str) -> str:
    """``_clean`` output with its newlines turned into separators.

    Empty lines are dropped rather than separated: a block written with a
    blank line between its parts would otherwise flatten to " ·  · ".

    A line that ends in a colon keeps a plain space after it, because it is a
    label for the line below rather than an item beside it. Without that the
    comparison link arrives as "See the full comparison: · https://..." --
    punctuation where a reader expects the thing being announced.
    """
    parts = [part for part in text.splitlines() if part]
    if not parts:
        return text
    out = parts[0]
    for part in parts[1:]:
        out += (" " if out.endswith(":") else _NEWLINE_AS) + part
    return out


class SalesDaddyWhatsAppProvider:
    channel = "whatsapp"
    provider_name = "salesdaddy"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.whatsapp_enabled
            and settings.salesdaddy_api_key
            and settings.whatsapp_template_name
        )

    def send(self, destination: Destination, message: RenderedMessage) -> SendResult:
        settings = get_settings()

        if not self.is_configured():
            return SendResult(
                ok=False, error_code="not_configured",
                error_detail="WhatsApp is not enabled or the Sales Daddy API key is missing",
                retryable=False,
            )
        if not destination.phone_e164:
            return SendResult(
                ok=False, error_code="no_phone",
                error_detail="Recipient has no E.164 phone number", retryable=False,
            )

        # Same chokepoint as the other two transports, so none of them can come
        # to disagree about which template carries a comparison.
        template, expected = whatsapp_template_for(message.kind, settings)
        if not template:
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

        # Sales Daddy does check the count (template_params_mismatch), but only
        # after the request is made; refusing here says why without a round
        # trip, and matches what the other providers do.
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

        payload = {
            "to": destination.phone_e164.lstrip("+"),
            "type": "template",
            "template": template,
            "language": settings.whatsapp_template_lang,
            # Meta's rules on a variable still apply underneath, so the same
            # cleaning as the direct Meta path -- AND ONE MORE THAT IS THIS
            # ROUTE'S ALONE.
            #
            # THE NEWLINE IS BACK ON THE FORBIDDEN LIST, FOR SALES DADDY.
            #
            # ``_clean`` deliberately keeps newlines: one was accepted on 9 Sep
            # 2026 and arrived laid out on the handset, and ``render`` has used
            # that ever since to make a slot a block. That measurement was
            # taken on the direct Meta path. It does not hold here.
            #
            # On 23 Sep 2026 the first price_change alert through an approved
            # template was accepted by Sales Daddy (200, status ``queued``,
            # id 3e28d284) and then rejected by Meta:
            #
            #     (#132018) There's an issue with the parameters in your template
            #
            # The same seven parameters with their newlines flattened, sent a
            # minute later (id 4db41013), reached the handset and were read.
            # Nothing else differed -- same template, same language, same
            # count, same rupee signs and arrows. The newline is the whole of
            # the difference.
            #
            # It matters most for the summary, whose every slot is a block by
            # design (``render._summary_params``), so that template would have
            # failed the same way the moment it was approved -- accepted, then
            # silently dropped, which is the failure this codebase has already
            # chased once.
            #
            # Here and not in ``render``: the message is the same message, and
            # which characters survive is a fact about the transport carrying
            # it. The email and the dashboard keep their line breaks.
            "params": [_one_line(_clean(p)) for p in params],
            # Creates or updates the lead in the Sales Daddy inbox, so the
            # conversation there is labelled with who it is.
            "name": destination.name,
        }
        url = f"{settings.salesdaddy_base_url.rstrip('/')}/v1/wa/send"

        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers={"X-Api-Key": settings.salesdaddy_api_key.get_secret_value()},
                )
        except httpx.HTTPError as exc:
            return SendResult(
                ok=False, error_code="network", error_detail=str(exc)[:500], retryable=True
            )

        return _classify(response)


def _classify(response: httpx.Response) -> SendResult:
    data = _safe_json(response)

    if response.is_success and data.get("ok") is True:
        message_id = data.get("messageId")
        log.info(
            "whatsapp_accepted",
            provider="salesdaddy",
            message_id=message_id,
            status=data.get("status"),
        )
        return SendResult(
            ok=True, provider_message_id=str(message_id)[:200] if message_id else None
        )

    code = str(data.get("error") or f"http_{response.status_code}")
    detail = str(data.get("message") or response.text or f"HTTP {response.status_code}")
    retry_after = data.get("retryAfter")
    if retry_after:
        detail = f"{detail} (retry after {retry_after}s)"

    retryable = code not in _PERMANENT_ERRORS and (
        response.status_code in (408, 429)
        or response.status_code >= 500
        or code in ("rate_limited", "whatsapp_error")
    )
    log.warning(
        "whatsapp_rejected",
        provider="salesdaddy",
        status=response.status_code,
        code=code,
        retryable=retryable,
    )
    return SendResult(
        ok=False, error_code=code[:60], error_detail=detail[:500], retryable=retryable
    )


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
