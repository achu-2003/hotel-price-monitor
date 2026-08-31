"""WhatsApp via My Dreams Technology, the reseller the client's number sits on.

HOW THIS DIFFERS FROM ``whatsapp_cloud``
=======================================
Same channel, same approved template, almost nothing else in common. The
reseller wraps Meta behind a handful of PHP endpoints:

  * everything is a **GET**, including the send;
  * credentials are ``LicenseNumber`` + ``APIKey`` **on the query string**;
  * there is no phone-number id -- the licence fixes which number sends;
  * there is no language code -- the template name alone selects the language;
  * template variables arrive as ONE comma-joined string, ``Param=a,b,c``;
  * **no delivery callback is documented**, so a send ends at "accepted" and
    the DELIVERED/READ half of the notification lifecycle never fires.

The business rule from Meta still applies underneath: a business-initiated
message must use a pre-approved template, so this provider sends a template
name plus positional parameters and never the rendered text.

"SUCCESS" MEANS ALMOST NOTHING HERE — MEASURED, NOT ASSUMED
===========================================================
Sending a template name that cannot exist::

    Template=zzz_definitely_not_a_real_template_9876
    -> {"ApiResponse":"Success","ApiMessage":"Message Received and Send to Meta"}

Byte-identical to the answer for a real one. The reseller validates nothing —
not the template name, not the parameter count — and reports success for
having received the HTTP request. The actual rejection happens at Meta,
afterwards, and no callback exists to report it.

So ``ok=True`` from this provider means "the reseller took it", and a
notification reaching SENT is *not* evidence that anything arrived, or even
that the template exists. Do not build a check on it, and do not read a run of
successful sends as a working integration. ``_PERMANENT_MARKERS`` below is
kept for the failures the reseller *can* report — a dead licence, a malformed
request — but on current evidence it will rarely fire.

When a message does not arrive, this provider cannot tell you why. The
reseller's own panel holds the Meta delivery status; the API does not expose
it.

THE COMMA
=========
``Param`` is split on commas by the reseller, so a comma *inside* a value
silently shifts every later variable into the wrong slot -- the alert then
reports a real number against the wrong label, with no error anywhere. This is
not an edge case: ``render.money`` uses Indian digit grouping, so every rate
above a thousand carries one.

    ₹12,000  ->  splits into "₹12" and "000"

URL-encoding does not help. ``%2C`` is decoded back to a comma before the
reseller splits on it, so the encoding round-trips and the field still breaks.
The only defence is to ensure no parameter contains a comma in the first place,
which is what ``_param_safe`` guarantees at a single chokepoint.

It lives here rather than in ``render`` on purpose: this is a limitation of one
transport, not a decision about what the message should say. The Meta path
keeps its digit grouping.
"""
from __future__ import annotations

import re

import httpx

from app.config import get_settings
from app.core.logging import get_logger
from app.notifications.base import (
    WHATSAPP_TEMPLATE_PARAM_COUNT,
    Destination,
    RenderedMessage,
    SendResult,
)

log = get_logger("notify.whatsapp.mydreams")

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)

#: Substrings that mean "this will fail identically forever".
#:
#: The reseller reports failures as prose, not as Meta's numeric codes, so
#: there is nothing to match on but the wording. Matching is done lowercased on
#: the whole body. Anything not listed is treated as retryable, which is the
#: safe direction: retrying a permanent failure wastes three attempts, while
#: giving up on a transient one loses the alert.
_PERMANENT_MARKERS = (
    "invalid licensenumber",
    "invalid apikey",
    "invalid license",
    "invalid api key",
    "authentication failed",
    "account expired",
    "account is expired",
    "licence expired",
    "license expired",
    "template not found",
    "invalid template",
    "template does not exist",
    "not approved",
    "invalid contact",
    "invalid number",
    "invalid mobile",
    "not a whatsapp",
    "parameter count",
    "insufficient",
)

#: Substrings that mean the reseller took the message.
#:
#: Checked case-insensitively. Kept deliberately narrow -- anything that does
#: not clearly say "sent" is treated as a failure rather than assumed good,
#: because a lost price alert is invisible and an over-reported one is not.
_SUCCESS_MARKERS = ("success", "submitted", "sent", "queued", "accepted", "ok")


class MyDreamsWhatsAppProvider:
    channel = "whatsapp"
    provider_name = "mydreams"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(
            settings.whatsapp_enabled
            and settings.mydreams_license_number
            and settings.mydreams_api_key
            and settings.whatsapp_template_name
        )

    def send(self, destination: Destination, message: RenderedMessage) -> SendResult:
        settings = get_settings()

        if not self.is_configured():
            return SendResult(
                ok=False, error_code="not_configured",
                error_detail="WhatsApp is not enabled or the My Dreams licence is missing",
                retryable=False,
            )
        if not destination.phone_e164:
            return SendResult(
                ok=False, error_code="no_phone",
                error_detail="Recipient has no E.164 phone number", retryable=False,
            )

        # Same contract as the Meta path, and refused for the same reason: the
        # approved template has a fixed number of variables, so any other count
        # is a message that is paid for and lands wrong. Reachable whenever a
        # notification rebuilds with no price_change rows behind it.
        params = message.template_params or []
        if len(params) != WHATSAPP_TEMPLATE_PARAM_COUNT:
            log.error(
                "whatsapp_param_count_mismatch",
                template=settings.whatsapp_template_name,
                expected=WHATSAPP_TEMPLATE_PARAM_COUNT,
                got=len(params),
            )
            return SendResult(
                ok=False,
                error_code="template_params",
                error_detail=(
                    f"Template {settings.whatsapp_template_name!r} takes "
                    f"{WHATSAPP_TEMPLATE_PARAM_COUNT} parameters, got {len(params)}"
                ),
                retryable=False,
            )

        query = {
            "LicenseNumber": settings.mydreams_license_number,
            "APIKey": settings.mydreams_api_key.get_secret_value(),
            # The reseller wants a bare country-code-prefixed number, as in
            # 919876543210 -- the E.164 '+' is not accepted.
            "Contact": destination.phone_e164.lstrip("+"),
            "Template": settings.whatsapp_template_name,
            "Param": ",".join(_param_safe(p) for p in params),
        }

        url = f"{settings.mydreams_base_url.rstrip('/')}/sendtemplate.php"
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.get(url, params=query)
        except httpx.HTTPError as exc:
            # str(exc) on an httpx error can carry the full request URL, and
            # the URL is where the API key is.
            return SendResult(
                ok=False, error_code="network",
                error_detail=_scrub(str(exc))[:500], retryable=True,
            )

        return _classify(response)


#: Ceiling on one parameter, matching the Meta path.
#:
#: The value that can realistically reach it is a room name, scraped off
#: somebody else's page and therefore untrusted in length as much as content.
_MAX_PARAM_CHARS = 700

#: A comma sitting between two digits, i.e. a thousands separator.
_GROUPING_COMMA = re.compile(r"(?<=\d),(?=\d)")


def _param_safe(value: object) -> str:
    """One comma-free line, never empty, bounded.

    The comma is the whole point -- see the module docstring. Two kinds occur
    and they want different treatment, distinguishable because a grouping comma
    always sits between two digits:

        ₹1,23,456            -> ₹123456     (separator dropped)
        Deluxe, Sea View     -> Deluxe; Sea View

    Dropping the digit grouping is a real loss: ``render.money`` goes to some
    trouble to write Indian rates the way an Indian reader expects, and this
    undoes it for this transport only. It is still the right trade -- an
    unglamorous ₹123456 says the true price, whereas ₹12 against the "old
    price" label says a false one. If the reseller can confirm an escape or an
    alternate delimiter, that belongs here and the grouping comes back.

    Newlines, tabs and runs of spaces are collapsed as well: those are rejected
    by Meta inside a template variable regardless of who transports it.
    """
    text = " ".join(str(value).split())
    text = _GROUPING_COMMA.sub("", text)
    text = text.replace(",", ";")
    text = " ".join(text.split()) or "—"
    if len(text) <= _MAX_PARAM_CHARS:
        return text
    return text[: _MAX_PARAM_CHARS - 1] + "…"


def _classify(response: httpx.Response) -> SendResult:
    """Turn whatever came back into a verdict.

    The reseller's response format is not in the API document, and wrappers of
    this shape commonly answer 200 with an error in the body -- so status code
    alone cannot be trusted, and neither can the presence of a body. Both JSON
    and plain text are handled.

    An unrecognised body is reported as a NON-retryable failure. That is
    deliberate and it is the least-bad of three bad options: retrying might
    send a real person the same alert repeatedly, and assuming success would
    lose it silently. Failing loudly and once puts the row in the notification
    list where an operator can see it and use the resend endpoint.
    """
    body = (response.text or "").strip()
    lowered = body.lower()

    # Structured errors first: if it is JSON and it names a status, believe it
    # over any keyword that happens to appear elsewhere in the body.
    payload = _safe_json(response)
    if payload:
        status_text = str(
            payload.get("status") or payload.get("Status") or payload.get("result") or ""
        ).lower()
        detail = str(
            payload.get("message")
            or payload.get("Message")
            or payload.get("description")
            or body
        )
        message_id = _message_id(payload)
        if status_text and any(m in status_text for m in _SUCCESS_MARKERS):
            # "accepted_unverified", not "sent": the reseller answers Success
            # for a template that does not exist, so this records what it said
            # rather than what happened. See the module docstring.
            log.info(
                "whatsapp_accepted_unverified", provider="mydreams", message_id=message_id
            )
            return SendResult(ok=True, provider_message_id=message_id)
        if status_text:
            return SendResult(
                ok=False,
                error_code=str(payload.get("code") or status_text)[:100],
                error_detail=_scrub(detail)[:500],
                retryable=_retryable(response.status_code, lowered),
            )

    if response.status_code != 200:
        return SendResult(
            ok=False,
            error_code=f"http_{response.status_code}",
            error_detail=_scrub(body)[:500] or f"HTTP {response.status_code}",
            retryable=_retryable(response.status_code, lowered),
        )

    # A permanent marker outranks a success word: "Template not found, message
    # not sent" contains both, and it is not a success.
    if any(marker in lowered for marker in _PERMANENT_MARKERS):
        return SendResult(
            ok=False, error_code="rejected",
            error_detail=_scrub(body)[:500], retryable=False,
        )
    if any(marker in lowered for marker in _SUCCESS_MARKERS):
        log.info("whatsapp_accepted", provider="mydreams", body=_scrub(body)[:200])
        return SendResult(ok=True, provider_message_id=_message_id(payload))

    log.error(
        "whatsapp_unrecognised_response",
        provider="mydreams",
        status=response.status_code,
        body=_scrub(body)[:500],
    )
    return SendResult(
        ok=False,
        error_code="unrecognised_response",
        error_detail=(
            "My Dreams returned a body this provider cannot classify, so the "
            f"message may or may not have been sent: {_scrub(body)[:300]!r}"
        ),
        retryable=False,
    )


def _retryable(status_code: int, lowered_body: str) -> bool:
    if any(marker in lowered_body for marker in _PERMANENT_MARKERS):
        return False
    return status_code == 429 or status_code >= 500 or status_code == 408


def _message_id(payload: dict) -> str | None:
    for key in ("messageid", "message_id", "msgid", "id", "MessageId", "MessageID"):
        value = payload.get(key)
        if value:
            return str(value)[:255]
    return None


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


#: Matches the two credential parameters anywhere in a string.
_SECRET_IN_URL = re.compile(r"(APIKey|LicenseNumber)=[^&\s]*", re.IGNORECASE)


def _scrub(text: str) -> str:
    """Strip credentials out of anything heading for a log or the database.

    The reseller authenticates on the query string, so the API key is present
    in the request URL -- and therefore in httpx's exception messages, which
    otherwise land verbatim in ``notifications.error_detail`` and in Sentry.
    """
    return _SECRET_IN_URL.sub(r"\1=***", text)
