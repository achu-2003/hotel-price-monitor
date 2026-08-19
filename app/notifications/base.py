"""The notification provider contract.

Same idea as the source adapter layer, at the other end of the pipeline: the
provider is the volatile part. Swapping SMTP for Resend, or Meta for Twilio,
must be a configuration change and must not touch the digest logic, the quiet
hours, or the delivery records.

A provider does exactly one thing: take a rendered message and a recipient,
try to deliver it, and report back. It does not decide WHETHER to send — that
decision belongs to the notify task, which knows about quiet hours, quotas and
deduplication.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ChangeLine:
    """One price change, in the form a message template needs.

    Flattened out of the ORM deliberately: the renderer and the providers are
    pure and testable, and a template can never trigger a lazy database load
    halfway through building an email.
    """

    hotel_name: str
    room_name: str
    old_price: Decimal | None
    new_price: Decimal | None
    delta: Decimal | None
    delta_pct: Decimal | None
    currency: str
    direction: str
    check_in: str
    check_out: str
    meal_plan: str | None = None
    # True when the two prices belong to consecutive stay dates rather than to
    # one night read twice. Without it a digest saying "1,023.75 -> 1,121.25"
    # reads as an intraday move, and the reader misjudges how fast the hotel
    # is repricing.
    is_overnight: bool = False

    @property
    def is_availability_event(self) -> bool:
        return self.direction in {"became_unavailable", "became_available"}


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A message ready to hand to a provider.

    Carries all three representations because the channels want different
    things: email takes ``html``, SMS-like channels take ``text``, and the
    WhatsApp Cloud API takes ``template_params`` because business-initiated
    messages must use a pre-approved template rather than free text.
    """

    subject: str
    text: str
    html: str | None = None
    template_params: list[str] | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    """What the provider managed to do.

    ``retryable`` is the provider's judgement and the task obeys it: a bounced
    address is permanent and retrying it three times only annoys the mail
    server, while a 502 from an API is worth another attempt.
    """

    ok: bool
    provider_message_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class Destination:
    """Where a message is going, without the rest of the recipient record.

    Providers get only what they need to deliver: no ids, no relationships,
    nothing that could end up in a provider's logs by accident.
    """

    name: str
    email: str | None = None
    phone_e164: str | None = None


@runtime_checkable
class NotificationProvider(Protocol):
    """Implemented by every delivery channel."""

    channel: str
    provider_name: str

    def is_configured(self) -> bool:
        """Whether this provider has everything it needs to send.

        Checked before a notification row is created, so a missing API key
        surfaces as a clear configuration error rather than a queue full of
        failed sends.
        """
        ...

    def send(self, destination: Destination, message: RenderedMessage) -> SendResult:
        ...
