"""Known booking engines, and recognising one from a URL.

WHY THIS EXISTS
===============
Attaching a hotel used to mean three manual decisions: pick the right source,
turn the pasted URL into a template with date placeholders, and paste a block
of JSON describing where the price lives. All three are mechanical, and all
three are derivable from the URL itself — the domain says which engine it is,
and the engine determines everything else.

So this module holds one profile per engine we have actually inspected, and
:func:`detect` turns a pasted booking URL into a complete, ready-to-fetch
configuration. Adding the twelfth hotel on a known engine becomes: paste URL.

WHAT A PROFILE IS NOT
=====================
It is not a guess. Every profile here was built from a real probe of a real
property: the endpoint was observed, the field paths read off the actual
payload, and the result checked against the prices shown on the page. An
engine nobody has inspected does not get a profile — it gets
``scripts/probe_site.py`` and a human. Inventing selectors for an unseen site
is exactly the guessing this system refuses to do everywhere else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

#: Query parameters that mean "check-in", across the engines seen so far.
#: Matching is case-insensitive and covers the common spellings, because every
#: engine names these slightly differently and all of them mean the same thing.
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "{check_in}": ("checkin", "check_in", "checkindate", "arrival", "arrivaldate",
                   "startdate", "fromdate", "from"),
    "{check_out}": ("checkout", "check_out", "checkoutdate", "departure",
                    "departuredate", "enddate", "todate", "to"),
    "{adults}": ("adults", "adult", "noofadults", "noofadult", "noofguests",
                 "guests", "numadults", "pax"),
    "{children}": ("children", "child", "noofkids", "kids", "noofchildren",
                   "numchildren"),
    "{rooms}": ("rooms", "room", "noofrooms", "numrooms"),
}


@dataclass(frozen=True, slots=True)
class EngineProfile:
    """Everything needed to fetch prices from one booking engine."""

    key: str
    display_name: str
    adapter_key: str
    #: Domain fragments that identify this engine in a booking URL.
    domains: tuple[str, ...]
    #: Field mapping and endpoint shape, verified against a real payload.
    adapter_config: dict[str, Any] = field(default_factory=dict)
    #: Pulls the engine's own property code out of the URL, when it carries one.
    external_id_pattern: str | None = None
    #: Politeness budget. Deliberately conservative for engines that serve many
    #: small properties from one host — the load lands on one server.
    rate_limit_per_min: int = 6
    notes: str = ""

    def matches(self, host: str) -> bool:
        host = host.lower()
        return any(domain in host for domain in self.domains)

    def external_id_from(self, url: str) -> str | None:
        if not self.external_id_pattern:
            return None
        match = re.search(self.external_id_pattern, url)
        return match.group(1) if match else None


#: One entry per engine that has been probed and verified end to end.
ENGINES: tuple[EngineProfile, ...] = (
    EngineProfile(
        key="aiosell",
        display_name="Aiosell booking engine",
        adapter_key="aiosell",
        domains=("aiosell.com",),
        # The adapter calls Aiosell's own rates API directly and needs no field
        # mapping: the payload shape is handled in code, because it cannot be
        # expressed as dotted paths (see app/adapters/aiosell.py).
        adapter_config={},
        external_id_pattern=r"/book/([A-Za-z0-9_-]+)",
        notes="Sellable rates via booking-engine-rates; server resolves occupancy.",
    ),
    EngineProfile(
        key="gotoyelagiri",
        display_name="gotoyelagiri portal",
        adapter_key="gotoyelagiri",
        domains=("gotoyelagiri.com",),
        # No field mapping: one shared response covers every resort on the
        # portal and the adapter filters it by resort id, which cannot be
        # expressed as dotted paths.
        adapter_config={},
        external_id_pattern=r"resort[_/=-]?(\d+)",
        notes="One call returns ~20 Yelagiri properties. Standing rates, not "
              "per-night pricing. Use scripts/seed_yelagiri.py to create them all.",
    ),
    EngineProfile(
        key="ezee-letsbook",
        display_name="eZee / letsbook.me booking engine",
        adapter_key="playwright_direct_site",
        domains=("letsbook.me", "ipms247.com"),
        # The rates API needs a Bearer token the page mints per visit, so the
        # page is loaded as a browser and the JSON IT requests is read. No
        # token is extracted or replayed.
        adapter_config={
            "json_url_contains": ["/booking/getAvailability"],
            "rooms_path": "data",
            "wait_timeout_ms": 45000,
            "fields": {
                "room_name": "roomName",
                "price_inclusive": "price.stayPriceAfterTax",
                "price_exclusive": "price.discountedStayPrice",
                "taxes_fees": "price.totalTaxes",
                # "0" reads as falsy in the mapping layer, so a sold-out room
                # is recorded as unavailable rather than as a missing price.
                "available": "availableRooms",
                "rooms_left": "availableRooms",
            },
        },
        external_id_pattern=r"hotelCode=(\d+)",
        notes="Storefront for eZee; prices arrive via the getAvailability XHR.",
    ),
)


@dataclass(frozen=True, slots=True)
class Detection:
    """A recognised URL, resolved into something that can be stored as-is."""

    profile: EngineProfile
    #: The pasted URL with its dates and occupancy turned into placeholders.
    url_template: str
    external_id: str | None
    #: Parameters that were replaced, for showing back to the operator.
    substituted: dict[str, str]

    @property
    def is_complete(self) -> bool:
        """Whether the template can actually vary by date.

        A URL with no recognisable date parameter would fetch the same night
        forever, which looks like it is working and is not.
        """
        return "{check_in}" in self.url_template


def parameterise_url(url: str) -> tuple[str, dict[str, str]]:
    """Replace date and occupancy values with placeholders.

    A pasted URL carries whatever dates the operator happened to be looking
    at. Stored verbatim it would pin the target to that one night forever —
    the checks would keep succeeding and the data would quietly go stale.

    Returns the templated URL and a map of what was replaced, so the dashboard
    can show its work rather than silently rewriting what was typed.
    """
    parts = urlparse(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        return url, {}

    lookup = {
        alias: placeholder
        for placeholder, aliases in _PARAM_ALIASES.items()
        for alias in aliases
    }

    substituted: dict[str, str] = {}
    rebuilt: list[tuple[str, str]] = []
    for key, value in pairs:
        placeholder = lookup.get(key.lower().replace("-", "").replace("_", ""))
        if placeholder and value:
            substituted[key] = f"{value} -> {placeholder}"
            rebuilt.append((key, placeholder))
        else:
            rebuilt.append((key, value))

    # safe="{}" keeps the placeholders readable instead of percent-encoding the
    # braces into %7B, which the adapter would not recognise.
    query = urlencode(rebuilt, safe="{}")
    return urlunparse(parts._replace(query=query)), substituted


def detect(url: str) -> Detection | None:
    """Recognise the booking engine behind a URL, or return ``None``.

    ``None`` means "not an engine we have inspected", which is a real answer
    and not a failure: it routes the operator to the probe rather than to a
    configuration invented on their behalf.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None

    for profile in ENGINES:
        if profile.matches(host):
            template, substituted = parameterise_url(url)
            return Detection(
                profile=profile,
                url_template=template,
                external_id=profile.external_id_from(url),
                substituted=substituted,
            )
    return None


def known_engines() -> list[dict[str, Any]]:
    """For the dashboard: what can be attached by pasting a URL alone."""
    return [
        {
            "key": e.key,
            "display_name": e.display_name,
            "adapter_key": e.adapter_key,
            "domains": list(e.domains),
            "notes": e.notes,
        }
        for e in ENGINES
    ]
