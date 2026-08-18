"""The source adapter contract.

Everything volatile about this system lives behind this interface. Sites
redesign, hotels switch booking engines, and a paid API might replace a
scraper later — none of which should touch the scheduler, the comparison
engine, the database, or the notifications.

An adapter has exactly one job: given a hotel and a stay window, return a list
of :class:`NormalizedOffer`. It does NOT decide whether a price changed, it
does NOT write to the database, and it does NOT notify anyone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.services.dates import StayWindow


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Everything an adapter needs to perform one fetch.

    The locale/timezone/currency fields are pinned deliberately. Hotel prices
    vary by the visitor's geography, device and session, so unless every fetch
    presents identical conditions we would be measuring our own variation
    rather than the hotel's pricing.
    """

    hotel_source_id: int
    hotel_name: str
    url: str | None
    external_id: str | None
    stay: StayWindow
    adults: int
    children: int = 0
    rooms: int = 1
    currency: str = "INR"
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"
    country: str = "IN"
    # Per-hotel overrides layered on the adapter YAML, so repairing a broken
    # adapter is a config edit rather than a deploy.
    config: dict[str, Any] = field(default_factory=dict)
    check_run_id: str | None = None

    @property
    def check_in(self) -> date:
        return self.stay.check_in

    @property
    def check_out(self) -> date:
        return self.stay.check_out


@dataclass(frozen=True, slots=True)
class NormalizedOffer:
    """One priceable room as returned by an adapter.

    Prices
    ------
    All three components are carried when the site provides them, because a
    hotel quoting "2,500 + taxes" and one quoting "2,950 inclusive" are the
    same price and must be comparable. The comparison basis is chosen once, at
    the series level, rather than guessed per fetch.

    Availability
    ------------
    A sold-out room is ``is_available=False`` with ``price=None``. It is NEVER
    a price of zero — that distinction is the difference between a correct
    "sold out" alert and an absurd "price dropped 100%" alert.
    """

    raw_room_name: str
    price_inclusive: Decimal | None = None
    price_exclusive: Decimal | None = None
    taxes_fees: Decimal | None = None
    currency: str = "INR"
    meal_plan: str | None = None
    refundable: bool | None = None
    is_available: bool = True
    rooms_left: int | None = None
    # Kept verbatim for debugging schema drift; scrubbed before it is stored.
    raw_payload: dict[str, Any] | None = None

    def price_on(self, basis: str) -> Decimal | None:
        """The number to compare on, for the configured basis.

        Falls back to the other component when only one was published, so a
        site that shows only an all-in price still produces a usable series.
        """
        if basis == "exclusive":
            return self.price_exclusive if self.price_exclusive is not None else self.price_inclusive
        return self.price_inclusive if self.price_inclusive is not None else self.price_exclusive

    def __post_init__(self) -> None:
        if self.is_available and self.price_inclusive is None and self.price_exclusive is None:
            # Not fatal here: the pipeline records the observation and refuses
            # to derive a change from it (see services/comparison.py). Raising
            # would throw away the other rooms found in the same page load.
            pass


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What one adapter run produced, plus how it went.

    ``offers`` being empty is meaningful and ambiguous, which is why
    ``sold_out_detected`` exists: "the page said no rooms available" is a
    business fact, while "we found nothing and do not know why" is a bug. An
    adapter that cannot tell the two apart must raise ``SchemaDriftError``
    rather than silently return an empty list.
    """

    offers: list[NormalizedOffer]
    sold_out_detected: bool = False
    duration_ms: int | None = None
    source_url: str | None = None
    notes: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    """Implemented by every price source.

    Adapters raise ``app.core.errors.FetchError`` subclasses on failure. The
    class of the error decides the retry policy, so raising the right one
    matters: ``BlockedError`` must never be retried, while ``TimeoutError_``
    should be.
    """

    adapter_key: str
    #: Which Celery queue this adapter belongs on. Browser work is heavy and
    #: memory-hungry, so it is isolated from light HTTP work.
    queue: str

    def fetch(self, context: FetchContext) -> FetchResult:
        """Return every priceable room for this hotel and stay window.

        One call per (hotel, stay window) — never one per room. A single page
        load already lists every room, so fetching per room would multiply the
        load on the site by 4-5x for no extra information.
        """
        ...
