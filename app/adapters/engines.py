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
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlparse, urlunparse

#: Query parameters that mean "check-in", across the engines seen so far.
#: Matching is case-insensitive and covers the common spellings, because every
#: engine names these slightly differently and all of them mean the same thing.
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "{check_in}": ("checkin", "check_in", "checkindate", "arrival", "arrivaldate",
                   "startdate", "fromdate", "from", "gindate", "indate"),
    "{check_out}": ("checkout", "check_out", "checkoutdate", "departure",
                    "departuredate", "enddate", "todate", "to", "goutdate",
                    "outdate"),
    "{adults}": ("adults", "adult", "noofadults", "noofadult", "noofguests",
                 "guests", "numadults", "pax"),
    "{children}": ("children", "child", "noofkids", "kids", "noofchildren",
                   "numchildren"),
    "{rooms}": ("rooms", "room", "noofrooms", "numrooms"),
}

#: Parameters that pack occupancy into ONE value, "adults-children" — Treebo's
#: ``?roomconfig=2-0``. They need their own table because the replacement is a
#: pattern rather than a whole value: left alone, the URL would keep asking for
#: two adults forever while the target said four, and every price would be
#: right for the wrong occupancy.
_COMBINED_OCCUPANCY_PARAMS: tuple[str, ...] = (
    "roomconfig", "roomconfigs", "occupancy", "paxconfig",
)
#: An ISO date sitting in the URL PATH rather than in a query parameter.
_ISO_DATE_IN_PATH_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_COMBINED_OCCUPANCY_RE = re.compile(r"^(\d+)-(\d+)$")


#: Date spellings seen in booking URLs, with the strftime pattern that
#: reproduces each. ISO first, because it is unambiguous.
_DATE_VALUE_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("%Y/%m/%d", re.compile(r"^\d{4}/\d{2}/\d{2}$")),
    ("%d-%m-%Y", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("%m-%d-%Y", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("%d/%m/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("%m/%d/%Y", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
)


#: A date written day-and-month-first in some order -- the shape whose reading
#: has to be settled before it can be rewritten. An ISO date is not in here:
#: there is nothing to settle about 2026-09-03.
_AMBIGUOUS_DATE_RE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{4}$")


def _date_format_of(check_in: str, check_out: str | None = None) -> str | None:
    """The strftime pattern the value or values are written in, or None.

    "03-09-2026" is 3 September to most of the world and 9 March to the United
    States, and a URL carries nothing that says which. Guessing wrong asks the
    hotel for a night six months from the one intended -- a plausible answer to
    the wrong question, which is the failure mode this codebase refuses
    everywhere else.

    THE PAIR RESOLVES IT. A stay has a check-out a night or two after its
    check-in, so only one reading of the pair describes a booking at all:

        03-09-2026 -> 04-09-2026    day-first:  3 Sep to 4 Sep, one night  OK
                                    month-first: 9 Mar to 9 Apr, 31 nights  no

    A pair that makes sense under neither reading, or under both, returns None
    and the caller leaves the dates alone. A source that cannot be re-dated is
    recorded as such, which is honest; one re-dated in the wrong dialect is
    not.

    "Under both" is rarer than it sounds but it is real, and February is where
    it lives:

        02-03-2026 -> 03-03-2026    day-first:   2 Mar to 3 Mar,  one night
                                    month-first: 3 Feb to 3 Mar,  28 nights

    Both are stays, so neither reading can be preferred and None is the answer.
    Two spellings that agree on the DATE are not a disagreement, though --
    03-03-2026 reads the same either way -- so the readings are compared by
    what they mean rather than by which pattern produced them.

    WITH ONLY A CHECK-IN, the pair is unavailable and the value must speak for
    itself: "25-12-2026" has no month 25 and so is day-first beyond argument,
    while "03-09-2026" has two readings and gets none. Some sites carry a
    check-in and a night count rather than a check-out, and a lone date that
    IS decidable should not be refused for the company it keeps.
    """
    readings: dict[tuple[date, date | None], str] = {}
    for fmt, pattern in _DATE_VALUE_FORMATS:
        if not pattern.match(check_in):
            continue
        try:
            start = datetime.strptime(check_in, fmt).date()
        except ValueError:
            continue
        # First pattern wins for a date two of them spell identically, so the
        # ISO forms listed above stay the ones that get reported.
        if check_out is None:
            readings.setdefault((start, None), fmt)
            continue
        if not pattern.match(check_out):
            continue
        try:
            end = datetime.strptime(check_out, fmt).date()
        except ValueError:
            continue
        if 1 <= (end - start).days <= 30:
            readings.setdefault((start, end), fmt)
    if len(readings) != 1:
        return None
    return next(iter(readings.values()))


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
        key="hotelzify",
        display_name="Hotelzify booking engine",
        adapter_key="hotelzify",
        domains=("booking.sterlingholidays.com", "hotelzify.com", "api.hotelzify.com"),
        # No field mapping: the payload cannot be expressed as dotted paths
        # and the price is not the one the API publishes.
        #
        # WHAT THE CONFIGURATION USED TO SAY, AND WHY IT WAS WRONG
        # =======================================================
        # This profile drove playwright_direct_site with:
        #
        #     price_exclusive: pricing[adultCount={adults}].priceForPax.0.priceBeforeTax
        #
        # which recorded Sterling Yelagiri at 10,093 on a night the page was
        # selling the same room for 3,859 -- 2.6x too high, every reading,
        # for as long as it watched. Three faults, none of them fixable in a
        # dotted path:
        #
        #   * the rate plan is a uuid on the entry and a name in a sibling
        #     dict, so matching on occupancy alone took the DEAREST plan
        #     (full board) rather than the Room Only rate the page leads with
        #   * childCount and infantCount went uncompared, so the right row
        #     was reached by list order rather than by logic
        #   * priceBeforeTax is the RACK rate. The discount a guest receives
        #     comes from a second endpoint the config could not call
        #
        # app/adapters/hotelzify.py does all three, and is testable against a
        # recorded payload. Same reasoning as aiosell above.
        adapter_config={},
        external_id_pattern=r"/rooms/(\d+)",
        notes="Direct JSON API, no browser: ~0.5s per fetch. Records the "
              "Room Only rate after live promotions -- the number printed on "
              "the page. Override the board with adapter_config.board.",
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
    EngineProfile(
        key="treebo",
        display_name="Treebo (brand site)",
        adapter_key="playwright_direct_site",
        domains=("treebo.com",),
        # DOM, not JSON -- and that is a rule here rather than a preference.
        #
        # Treebo's prices come from /api/v1/checkout_v2/.../room-prices/ and
        # /api/v8/pricing/hotels/, and its robots.txt carries a blanket
        # "Disallow: /api/". The hotel page is explicitly allowed; the
        # endpoints behind it are not. So `json_url_contains` is deliberately
        # absent: configuring it would read exactly what we have been asked
        # not to. If a later probe finds an allowed JSON route, that is the
        # upgrade -- until then the DOM is the only permitted surface.
        adapter_config={
            "wait_for": "#t-roomTypes",
            # Treebo hashes every CSS class (styled-components: "sc-c8jr3n-0
            # jaYzsn"), and those change on each deploy. What survives is the
            # handful of semantic ids the site sets itself -- t-roomTypes,
            # t-mainFooter, t-qna-question -- so the card is anchored on one
            # of those, and the fields inside it on their own text.
            "room_card": "#t-roomTypes",
            "selectors": {
                # Playwright's text engine matches the SMALLEST element
                # containing the match, which is what makes these usable: a
                # CSS ancestor selector would return the whole card and store
                # "chevron_left chevron_right 10 Photos Deluxe Room (Maple)
                # 150 sq.ft. ..." as the room name.
                "room_name": r"text=/Room \(/",
                "price": r"text=/^₹\s?[\d,]+$/",
            },
            "sold_out_markers": [
                "no rooms available",
                "sold out",
                "fully booked",
            ],
        },
        # The property code is the last number in the slug:
        # /hotels-in-yelagiri/itsy-hotels-kurinji-stay-inn-...-3965/
        external_id_pattern=r"-(\d+)/?(?:[?#]|$)",
        # Slower than the engines above: this is one brand's own site rather
        # than a multi-tenant engine, and there is no reason to lean on it.
        rate_limit_per_min=3,
        notes=(
            "ONE room type per check, not the full list. The page shows only "
            "the default (cheapest) room and hides the rest behind a 'View "
            "All Rooms' control, so the series is this hotel's headline rate. "
            "Prices are quoted tax-INCLUSIVE ('Incl. tax for 1 night'), which "
            "the card's own wording tells the parser. Expect some session "
            "variance: coupon and prepaid promotions move the figure by tens "
            "of rupees between loads, which the alert thresholds absorb."
        ),
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

        A date placeholder may carry the engine's own spelling of a date --
        "{check_in:%d-%m-%Y}" for a site that asks for 03-09-2026 -- so the
        prefix is what is tested. Matching the bare form alone declared a
        perfectly re-datable URL a standing rate, which then pinned it to the
        night the operator happened to paste.
        """
        return (
            "{check_in}" in self.url_template
            or "{check_in:" in self.url_template
        )


def _placeholder_for_each(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Which placeholder, if any, should replace each parameter's value.

    Keyed by the parameter name exactly as the URL writes it, so a caller can
    rewrite either a parsed query or the raw text of a fragment without
    restating any of the reasoning below.
    """
    lookup = {
        alias: placeholder
        for placeholder, aliases in _PARAM_ALIASES.items()
        for alias in aliases
    }

    # Which dialect this engine writes its dates in, decided from the PAIR
    # before anything is rewritten -- see _date_format_of. Left as None when
    # the dates are already ISO, in which case the placeholders render the
    # default and the behaviour is unchanged.
    date_values: dict[str, str] = {}
    for key, value in pairs:
        placeholder = lookup.get(key.lower().replace("-", "").replace("_", ""))
        if placeholder in ("{check_in}", "{check_out}") and value:
            date_values.setdefault(placeholder, value)

    date_format: str | None = None
    # A day-and-month date whose order cannot be settled. The dates are then
    # left exactly as pasted: substituting the plain placeholder would render
    # ISO, which is a DIFFERENT wrong answer from the one just refused -- the
    # site would be asked for 2026-03-02 when it spells that date 02-03-2026.
    # Leaving them alone makes is_complete false, so the source is reported as
    # unable to price a specific night and a person is asked. That is the
    # honest end of an unanswerable question.
    dates_are_ambiguous = False
    if date_values:
        detected = _date_format_of(
            date_values.get("{check_in}") or date_values["{check_out}"],
            date_values.get("{check_out}") if "{check_in}" in date_values else None,
        )
        if detected is None:
            dates_are_ambiguous = any(
                _AMBIGUOUS_DATE_RE.match(value) for value in date_values.values()
            )
        elif detected != "%Y-%m-%d":
            date_format = detected

    resolved: dict[str, str] = {}
    for key, value in pairs:
        placeholder = lookup.get(key.lower().replace("-", "").replace("_", ""))
        if placeholder in ("{check_in}", "{check_out}"):
            if dates_are_ambiguous:
                continue
            if date_format:
                placeholder = placeholder[:-1] + ":" + date_format + "}"
        if placeholder and value:
            resolved[key] = placeholder
    return resolved


#: One ``key=value`` inside a fragment's query. The value may itself contain
#: "=" -- swiftbook's propertyId is base64 and ends in one -- so it runs to the
#: next "&" rather than to the next "=".
_FRAGMENT_PAIR_RE = re.compile(r"(?P<key>[^=&]+)=(?P<value>[^&]*)")


def _parameterise_fragment(fragment: str) -> tuple[str, dict[str, str]]:
    """The same substitution, for a URL that keeps its parameters after the #.

    Single-page booking engines route on the hash. swiftbook hands out

        https://www.swiftbook.io/inst/#home?propertyId=...&checkIn=2026-08-25

    where everything that matters is in the FRAGMENT. ``urlparse`` puts none of
    that in ``query``, so the scan found no date parameter, the URL was stored
    with 25 August baked into it, and ``is_complete`` reported a source that
    cannot price a specific night -- of a site that prices per night perfectly
    well. Every check would then re-read one night forever while succeeding.

    Rewritten as TEXT, not parsed and re-encoded. A fragment is read by the
    site's own JavaScript rather than by a server, and it carries values that
    survive only if left byte-for-byte alone: percent-encoding the "=" that
    ends a base64 propertyId is exactly the kind of helpfulness that produces a
    URL the page cannot open. Only the values being replaced are touched.
    """
    route, separator, query = fragment.partition("?")
    if not separator:
        return fragment, {}
    pairs = parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return fragment, {}
    by_key = _placeholder_for_each(pairs)
    if not by_key:
        return fragment, {}

    substituted: dict[str, str] = {}

    def replace(match: "re.Match[str]") -> str:
        key, value = match.group("key"), match.group("value")
        placeholder = by_key.get(key)
        if not placeholder or not value:
            return match.group(0)
        substituted[key] = f"{value} -> {placeholder}"
        return f"{key}={placeholder}"

    return route + "?" + _FRAGMENT_PAIR_RE.sub(replace, query), substituted


def parameterise_url(url: str) -> tuple[str, dict[str, str]]:
    """Replace date and occupancy values with placeholders.

    A pasted URL carries whatever dates the operator happened to be looking
    at. Stored verbatim it would pin the target to that one night forever —
    the checks would keep succeeding and the data would quietly go stale.

    Returns the templated URL and a map of what was replaced, so the dashboard
    can show its work rather than silently rewriting what was typed.
    """
    parts = urlparse(url)
    substituted: dict[str, str] = {}
    path = parts.path

    # Dates are not always query parameters. bookmystay.io puts them in the
    # path -- /rooms/43046/2026-08-19/2026-08-20/2/0 -- and a URL whose dates
    # were invisible to this function was stored with them baked in AND marked
    # as a source that cannot price a specific night, because is_complete asks
    # whether a {check_in} placeholder came out of here. Both of those are
    # wrong for a site that prices per night perfectly well.
    #
    # Only ISO dates are recognised, and only the first two: they are
    # unambiguous, and a path segment that looks like 2026-08-19 is a date on
    # every booking site anyone has pasted so far.
    iso_dates = _ISO_DATE_IN_PATH_RE.findall(path)
    if len(iso_dates) >= 2:
        path = path.replace(iso_dates[0], "{check_in}", 1).replace(iso_dates[1], "{check_out}", 1)
        substituted["path date 1"] = f"{iso_dates[0]} -> {{check_in}}"
        substituted["path date 2"] = f"{iso_dates[1]} -> {{check_out}}"
    elif len(iso_dates) == 1:
        path = path.replace(iso_dates[0], "{check_in}", 1)
        substituted["path date"] = f"{iso_dates[0]} -> {{check_in}}"

    # ...nor are they always in the query string. A single-page booking engine
    # routes on the hash and puts everything after it -- see
    # _parameterise_fragment, which is where the whole of swiftbook's URL
    # lives.
    fragment, from_fragment = _parameterise_fragment(parts.fragment)
    substituted.update(from_fragment)

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        return (
            urlunparse(parts._replace(path=path, fragment=fragment)),
            substituted,
        )

    rebuilt: list[tuple[str, str]] = []
    # Exactly the values written by this loop, so the encoder below can spare
    # those and only those.
    placeholder_values: set[str] = set()
    by_key = _placeholder_for_each(pairs)
    for key, value in pairs:
        normalised = key.lower().replace("-", "").replace("_", "")
        placeholder = by_key.get(key)
        combined = (
            _COMBINED_OCCUPANCY_RE.match(value)
            if normalised in _COMBINED_OCCUPANCY_PARAMS and value
            else None
        )
        if placeholder and value:
            substituted[key] = f"{value} -> {placeholder}"
            placeholder_values.add(placeholder)
            rebuilt.append((key, placeholder))
        elif combined:
            substituted[key] = f"{value} -> {{adults}}-{{children}}"
            placeholder_values.add("{adults}-{children}")
            rebuilt.append((key, "{adults}-{children}"))
        else:
            rebuilt.append((key, value))

    # Placeholders are written through untouched; everything else is encoded
    # normally.
    #
    # urlencode's "safe" is the obvious tool and it is the wrong one here,
    # because it applies to EVERY value. A date placeholder carries its
    # strftime pattern -- "{check_in:%d-%m-%Y}" -- so sparing it means sparing
    # "%", and a real query value containing a literal percent then comes out
    # as an escape sequence that reads back as a different string. The
    # placeholders are the ones this function just wrote, so they are known
    # exactly and need no exemption applied to anyone else's data.
    query = "&".join(
        f"{quote_plus(key)}={value if value in placeholder_values else quote_plus(value)}"
        for key, value in rebuilt
    )
    return (
        urlunparse(parts._replace(path=path, query=query, fragment=fragment)),
        substituted,
    )


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
