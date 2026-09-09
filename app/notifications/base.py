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
    # one night read twice.
    #
    # No longer printed. ``render._headline`` used to append "vs last night",
    # and it was four words on a line the reader scans for two numbers -- on
    # WhatsApp, where every move is one clause of a run-on paragraph, repeating
    # it cost more than the distinction was worth. Carried on the line anyway,
    # because it is a fact about the pair of prices and the /changes page still
    # shows it as a pill: a renderer that wants it back needs no new plumbing.
    is_overnight: bool = False

    # Set when the two prices on this line are not both on the basis the
    # Settings switch asked for -- "incl. tax" on a rate the site publishes
    # only all-in, "excl. tax" on one it publishes only pre-tax, or
    # "mixed tax basis" when the two sides disagree with each other.
    #
    # None on the ordinary line, deliberately. A marker on every line is noise
    # the reader learns to skip, and then it is not there on the one line that
    # needed it. Decided in ``services/price_display.py``, which is the same
    # rule the matrix marks its cells with.
    basis_note: str | None = None

    @property
    def is_availability_event(self) -> bool:
        return self.direction in {"became_unavailable", "became_available"}


#: How many body variables the approved WhatsApp template has.
#:
#: ``render._whatsapp_params`` produces exactly this many and the provider
#: refuses to send any other number. The two must agree: Meta answers a wrong
#: count with error 132000, which is classified permanent, so a drift here is a
#: paid message that can never arrive and is never retried.
WHATSAPP_TEMPLATE_PARAM_COUNT = 7

#: The FEWEST body variables the market-summary template can have.
#:
#: Two of them are fixed -- how many rooms moved and over what window, and the
#: time -- and at least one carries the moves themselves. See
#: ``render.render_summary``.
#:
#: A LARGER TEMPLATE CARRIES MORE MOVES, WHICH IS THE POINT
#: ========================================================
#: Meta caps a single body variable, and both providers cut one at 700
#: characters. A busy afternoon across four properties does not fit in one, so
#: every variable above the fixed two holds another slice of the window and the
#: renderer spreads the moves across them.
#:
#: The count is configuration (``whatsapp_comparison_template_params``) rather
#: than a constant, because the approval is somebody else's decision. But do
#: not ask Meta for the largest template it will grant here: a template is
#: refused when it has too many variables for the length of its own text
#: (error 2388293), and this message has little text to spare. Five is the
#: shape this deployment settled on.
WHATSAPP_COMPARISON_MIN_PARAMS = 3

#: Kept under the old name for callers that only need the default.
WHATSAPP_COMPARISON_PARAM_COUNT = WHATSAPP_COMPARISON_MIN_PARAMS

#: What a message is ABOUT, which decides which approved template carries it.
#:
#: The same two words appear on ``notifications.kind``, and deliberately so: a
#: notification held overnight is rebuilt from its row hours later, and the
#: rebuild has to produce the same kind of message that was queued. Deriving it
#: instead -- "no hotel_id, so it must be a summary" -- would silently
#: reclassify every ops alert.
PRICE_CHANGE = "price_change"
MARKET_COMPARISON = "market_comparison"


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """A message ready to hand to a provider.

    Carries all three representations because the channels want different
    things: email takes ``html``, SMS-like channels take ``text``, and the
    WhatsApp Cloud API takes ``template_params`` because business-initiated
    messages must use a pre-approved template rather than free text.

    ``kind`` says which approved template the parameters belong to. It travels
    on the message rather than being worked out by the provider, because the
    provider sees a list of strings either way and cannot tell a price move
    from a market summary by looking at them -- and putting one through the
    other's template is a paid message that lands wrong.
    """

    subject: str
    text: str
    html: str | None = None
    template_params: list[str] | None = None
    kind: str = PRICE_CHANGE


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


def whatsapp_template_for(kind: str, settings) -> tuple[str, int]:
    """The template name and expected parameter count for a message kind.

    One chokepoint, used by both WhatsApp providers, so the reseller path and
    the Meta path cannot come to disagree about which template carries what.

    An empty name means the template has not been approved on this deployment
    yet. The providers refuse the send and say so, rather than falling back to
    the other template: the fallback would be accepted by Meta, charged for,
    and delivered as a price-change alert with a whole market summary stuffed
    into its seven slots -- which reads as a real alert about a room that did
    not move.
    """
    if kind == MARKET_COMPARISON:
        return (
            settings.whatsapp_comparison_template_name,
            settings.whatsapp_comparison_template_params,
        )
    return settings.whatsapp_template_name, WHATSAPP_TEMPLATE_PARAM_COUNT
