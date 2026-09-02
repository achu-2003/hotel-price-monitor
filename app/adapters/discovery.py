"""Work out where a site keeps its prices, from nothing but a URL.

WHY THIS EXISTS
===============
Every booking engine needed a hand-written profile: someone opened the site,
watched the network tab, found the availability payload and wrote down the
field paths. That is fine for the fourth hotel on an engine you already know
and useless for the first hotel on an engine you do not.

This does that inspection automatically. It drives the page, captures every
JSON response, and looks for the shape a room list always has underneath the
naming differences: an array of objects where one field is a room name and
another is a plausible nightly rate.

THE RULE THAT MAKES IT TRUSTWORTHY
==================================
**A candidate is only accepted if its prices also appear on the rendered page.**

Discovery on its own is pattern-matching, and pattern-matching invents things.
A payload might carry a "price" field that is a deposit, a tax, a per-person
supplement or last year's rate — all numerically plausible, all wrong. So every
candidate is cross-checked against the text a guest actually sees, and one that
cannot be corroborated is reported as unverified rather than returned as a
finding. A missing config is a visible gap; a wrong one silently poisons a
price series for months.

WHAT IT DOES NOT DO
===================
It does not decide whether we may fetch the site — robots.txt and the Terms of
Service review are separate, and neither is a technical question. It does not
write anything to the database. It reports what it found and how sure it is;
acting on that is the caller's decision.
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from app.adapters.parsing import (
    CURRENCY_ICON_SELECTOR,
    MAX_PLAUSIBLE_PRICE,
    MIN_PLAUSIBLE_PRICE,
    looks_sold_out,
)
from app.core.logging import get_logger

log = get_logger("discovery")

#: How long to wait for a page to stop fetching things before giving up on
#: quiet and inspecting it as it stands. Long enough for a booking widget to
#: finish its availability call; short enough that a page with a permanent
#: heartbeat -- most large OTAs -- costs a quarter of a minute rather than
#: the whole navigation budget.
_SETTLE_MS = 15_000

#: Field names that tend to hold the room's name. Ordered: an exact hit on an
#: earlier entry beats a fuzzy hit on a later one.
_NAME_HINTS = (
    "roomname", "room_name", "displayname", "display_name", "roomtype",
    "room_type", "title", "name", "label", "roomtitle", "category",
)

#: Field names that tend to hold what the guest pays. "total" and "amount" sit
#: late because they are also used for deposits and whole-stay figures.
_PRICE_HINTS = (
    "sellrate", "sellprice", "netprice", "finalprice", "discountedprice",
    "price", "rate", "totalrate", "amount", "total", "nightlyrate", "perNight",
)

#: Fields to never treat as the price, whatever their value looks like. Each of
#: these has been seen holding a number in the right range and the wrong
#: meaning.
_PRICE_BLOCKLIST = (
    "originalprice", "original_rate", "rackrate", "strikeprice", "oldprice",
    "mrp", "tax", "taxes", "gst", "deposit", "discountamount", "saving",
    "extraadult", "extrachild", "childprice", "id", "roomid", "hotelcode",
    "pincode", "phone", "rating", "reviews", "distance",
)

_AVAILABLE_HINTS = ("isavailable", "available", "isactive", "instock", "bookable")
_COUNT_HINTS = ("availablerooms", "available_rooms", "roomsleft", "rooms_left",
                "allocation", "inventory", "totalcount", "quantity", "stock")

#: A room name is a short human phrase. Numbers-only or a paragraph are not.
#: Any segment that is a literal date. A path through one is correct today
#: and returns nothing tomorrow -- the checks keep "succeeding" while the
#: prices silently stop updating, which is the worst failure this system has.
_DATED_SEGMENT = re.compile(r"(^|\.)\d{4}-\d{2}-\d{2}(\.|$)|(^|\.)\d{8}(\.|$)")

_NAME_RE = re.compile(r"^[A-Za-z][\w\s\-/&'(),.+]{2,79}$")
_ROOMY = re.compile(
    r"\b(room|suite|deluxe|standard|superior|premium|executive|cottage|villa|"
    r"studio|tent|dorm|family|double|twin|triple|single|apartment|bed|"
    r"queen|king|balcony|view|ac|non\s*ac)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Candidate:
    """One possible room list, with what is needed to read it again.

    Covers both routes. A JSON candidate carries dotted paths into a captured
    payload; a DOM candidate carries CSS selectors into the rendered page. They
    are scored and corroborated identically, so the caller does not care which
    one won — only that its prices were confirmed against the page.
    """

    source_url: str
    rooms_path: str
    fields: dict[str, str]
    #: "json" or "dom". Decides which adapter_config shape is produced.
    kind: str = "json"
    sample_names: list[str] = field(default_factory=list)
    #: DOM only, and usually empty. The rate plan read from each sampled card,
    #: aligned with ``sample_names``, when the scan found a selector that tells
    #: two cards of one room apart. Empty everywhere else, which is every page
    #: whose rooms are each named once.
    sample_plans: list[str] = field(default_factory=list)
    sample_prices: list[Decimal] = field(default_factory=list)
    room_count: int = 0
    #: How many of the prices were also found in the page's visible text.
    corroborated: int = 0
    #: Of those, how many were printed with a currency beside them. A bare
    #: number corroborates against whatever printed it -- a room size, a guest
    #: count, a distance to the beach -- so this is the count that actually
    #: distinguishes a rate from a coincidence. See :meth:`is_strongly_verified`.
    corroborated_marked: int = 0
    score: float = 0.0
    #: DOM only. Whether the element the names came from is a heading or a
    #: self-declared name container, as opposed to something that merely scored
    #: well. Decides whether several rooms sharing one name reads as rate plans
    #: or as a broken selector — see :meth:`is_verified`.
    name_trusted: bool = True

    @property
    def is_verified(self) -> bool:
        """At least half the prices appear on the page, and the rooms have
        distinct names.

        Half rather than all for the prices: a page often shows only the
        cheapest rate per room, or hides sold-out rooms, so demanding every
        price would reject good candidates. Demanding none would accept
        invented ones.

        The names condition exists because corroboration alone cannot see this
        failure. A selector that lands on a shared amenity chip -- "King Size
        Bed" on all six cards of one real hotel -- yields prices that ARE on
        the page and so passes every check here, while naming six different
        rooms identically. Downstream they collapse into one room type and five
        of the six are dropped as duplicate offer keys, which is how a
        six-room property came to be monitored as a single room.

        Repetition on its own is NOT enough to condemn a candidate, though. A
        property with one room type and three rate plans genuinely lists three
        cards all reading "Deluxe Room", and refusing that would reject a site
        that is working perfectly. What separates the two is where the name
        came from: ``name_trusted`` is set when the scan took it from a heading
        or a container that calls itself a name, which a rate-plan list does
        and an amenity chip does not.

        So: several rooms sharing one name is accepted from a trusted element
        and refused from an untrusted one. One sampled room is exempt either
        way -- there is nothing for it to differ from.

        All of that is about NAMES because, until the scan learned to find a
        rate plan, a name was the whole of an offer's identity. Where a plan
        selector was found it is not: the offers will be filed under (name,
        plan), and that is what has to tell the rooms apart. A shared name the
        plan separates is then no longer a collapse, and counting names would
        refuse a candidate that is about to work perfectly.

        PARTIAL repetition counts too, and asking only whether ALL the names
        were identical missed the case that matters most. A booking engine
        labelling each rate row with its category gave eight cards reading
        "Room", "Room", "Room", "Room", "Villa", "Villa", "Room", "Room": not
        one name, so the check passed, and a seven-room property was written
        into live configuration as two. The test is therefore whether the names
        tell the rooms APART -- fewer than half of them distinct is a label,
        not a room list.
        """
        if not self.sample_prices or self.corroborated * 2 < len(self.sample_prices):
            return False
        identities = self.sample_identities
        if (
            not self.name_trusted
            and len(identities) > 1
            and len(set(identities)) * 2 <= len(identities)
        ):
            return False
        return True

    @property
    def sample_identities(self) -> list[str]:
        """What the sampled offers will actually be filed under.

        The name alone where no plan selector was found, which is every page
        that needed none. A card whose plan came back empty keeps its name and
        an empty plan, exactly as the fetch will key it.
        """
        plans = self.sample_plans or []
        return [
            f"{name}\x1f{plans[i] if i < len(plans) else ''}"
            for i, name in enumerate(self.sample_names)
        ]

    @property
    def is_strongly_verified(self) -> bool:
        """Verified, AND at least one price was printed with a currency.

        The bar for OVERWRITING a configuration a monitor is already running
        on, where :meth:`is_verified` is the bar for proposing one to a person.
        The two differ because the failure they have to survive differs: a
        first-time discovery is read by whoever pasted the URL, and an
        automatic repair is read by nobody.

        What makes ``is_verified`` insufficient on its own is that
        corroboration cannot tell a price from the number that happens to sit
        where a price would. "Room Size 134 m2" contains 134; a candidate that
        called 134 a price was confirmed against the page, because 134 really
        is on the page. Requiring a currency marker beside it is the smallest
        check that separates the two, and it is the one booking pages always
        satisfy for real rates -- printing money without saying which money is
        not something a page selling rooms does.

        A page that prints no currency anywhere therefore cannot produce an
        automatic repair at all. That is deliberate: on such a page nothing
        distinguishes a rate from any other three-digit number, and the
        outstanding alert -- which sends a person to look -- is a better
        outcome than a confident guess that survives until someone notices.
        """
        return self.is_verified and self.corroborated_marked > 0

    def as_adapter_config(self, json_fragment: str) -> dict[str, Any]:
        if self.kind == "dom":
            # CSS selectors are far more fragile than a JSON contract: a
            # restyle breaks them where an API shrugs. That is not a reason to
            # refuse — it is a reason the adapter raises SchemaDriftError with
            # a screenshot instead of writing a guessed number.
            return {
                "room_card": self.rooms_path,
                "wait_for": self.rooms_path,
                "wait_timeout_ms": 45000,
                "selectors": dict(self.fields),
            }
        return {
            "json_url_contains": [json_fragment],
            "rooms_path": self.rooms_path,
            "wait_timeout_ms": 45000,
            "fields": dict(self.fields),
        }


@dataclass(slots=True)
class DiscoveryResult:
    best: Candidate | None
    others: list[Candidate] = field(default_factory=list)
    page_prices: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Set when the page itself could not be read from, as opposed to read and
    #: found wanting -- see :func:`why_the_page_cannot_be_learned`. Carried as
    #: a field rather than left in ``notes`` because a caller has to act on the
    #: distinction: "come back when this hotel has a room to sell" is not a
    #: failed attempt, and must not be charged as one.
    unlearnable: str | None = None

    @property
    def ok(self) -> bool:
        return self.best is not None and self.best.is_verified


def json_fragment(url: str) -> str:
    """A stable slice of an endpoint URL, for matching it again next fetch.

    The path without the query: query strings carry dates and ids that change
    every run, so matching on the whole URL would match nothing tomorrow.

    Lives here, beside :meth:`Candidate.as_adapter_config` which consumes it,
    because both callers that build an adapter config from a discovery result
    — attaching a source and repairing one — have to derive this identically.
    Two copies that drifted apart would produce two configs for one site.
    """
    from urllib.parse import urlparse

    path = urlparse(url).path or url
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts[-2:]) if len(parts) >= 2 else path


# ── walking the payload ──────────────────────────────────────────────
def _iter_arrays(node: Any, path: str = "", depth: int = 0):
    """Yield every (path, list-of-dicts) in a payload.

    Room lists hide at wildly different depths — top level, under ``data``,
    under ``data.hotel.roomTypes`` — so every array is considered and scored
    rather than guessed at by name.
    """
    if depth > 6:
        return
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            yield path, node
        for index, item in enumerate(node[:3]):
            yield from _iter_arrays(item, f"{path}.{index}" if path else str(index), depth + 1)
    elif isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            yield from _iter_arrays(value, child, depth + 1)


def _flatten(obj: dict, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Flatten one room object to dotted paths, so nested prices are reachable."""
    out: dict[str, Any] = {}
    if depth > 3:
        return out
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path, depth + 1))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            # Rates are often a list; the first entry is the one shown first.
            out.update(_flatten(value[0], f"{path}.0", depth + 1))
        else:
            out[path] = value
    return out


def _hint_rank(path: str, hints: tuple[str, ...]) -> int | None:
    """Position of the first hint matching the LAST segment of a path."""
    leaf = path.rsplit(".", 1)[-1].lower().replace("_", "").replace("-", "")
    for index, hint in enumerate(hints):
        if hint.replace("_", "") in leaf:
            return index
    return None


def _looks_like_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = " ".join(value.split())
    return bool(_NAME_RE.match(text))


def _as_price(value: Any) -> Decimal | None:
    """A number inside the plausible range for one night in a room."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except Exception:  # noqa: BLE001 - any unparseable value is simply not a price
        return None
    return amount if MIN_PLAUSIBLE_PRICE <= amount <= MAX_PLAUSIBLE_PRICE else None


# ── scoring one array ────────────────────────────────────────────────
def _evaluate(url: str, path: str, rows: list[dict]) -> Candidate | None:
    """Decide whether an array is a room list, and which fields matter."""
    sample = [r for r in rows[:12] if isinstance(r, dict)]
    if not sample:
        return None
    flat = [_flatten(row) for row in sample]
    keys = {k for row in flat for k in row}

    # -- the name field --
    name_path, name_rank, roomy_hits = None, 99, -1
    for key in sorted(keys):
        if _DATED_SEGMENT.search(key):
            continue
        rank = _hint_rank(key, _NAME_HINTS)
        values = [row.get(key) for row in flat]
        if not all(_looks_like_name(v) for v in values if v is not None):
            continue
        present = [v for v in values if isinstance(v, str) and v.strip()]
        if len(present) < max(1, len(flat) // 2):
            continue
        # Distinct values matter: a field repeating one string is a category,
        # not a room name.
        if len({v.strip().lower() for v in present}) < max(2, len(present) // 2) and len(present) > 2:
            continue
        roomy = sum(1 for v in present if _ROOMY.search(v))
        effective = rank if rank is not None else 50
        if (roomy, -effective) > (roomy_hits, -name_rank):
            name_path, name_rank, roomy_hits = key, effective, roomy

    if name_path is None:
        return None

    # -- the price field --
    price_path, price_rank = None, 99
    for key in sorted(keys):
        leaf = key.rsplit(".", 1)[-1].lower().replace("_", "")
        if any(bad.replace("_", "") in leaf for bad in _PRICE_BLOCKLIST):
            continue
        if _DATED_SEGMENT.search(key):
            # Keyed by the date being asked about. Usable once, useless from
            # tomorrow, and the failure is silent.
            continue
        rank = _hint_rank(key, _PRICE_HINTS)
        if rank is None:
            continue
        prices = [_as_price(row.get(key)) for row in flat]
        if sum(1 for p in prices if p is not None) < max(1, len(flat) // 2):
            continue
        if rank < price_rank:
            price_path, price_rank = key, rank

    if price_path is None:
        return None

    fields = {"room_name": name_path, "price_inclusive": price_path}
    for hint_set, target in ((_AVAILABLE_HINTS, "available"), (_COUNT_HINTS, "rooms_left")):
        best, best_rank = None, 99
        for key in sorted(keys):
            if _DATED_SEGMENT.search(key):
                continue
            rank = _hint_rank(key, hint_set)
            if rank is not None and rank < best_rank:
                best, best_rank = key, rank
        if best:
            fields[target] = best

    names = [str(row.get(name_path)).strip() for row in flat if row.get(name_path)]
    prices = [p for p in (_as_price(row.get(price_path)) for row in flat) if p is not None]

    candidate = Candidate(
        source_url=url,
        rooms_path=path,
        fields=fields,
        sample_names=names[:8],
        sample_prices=prices[:8],
        room_count=len(rows),
    )
    # Prefer: recognisable room words, a well-known field name, more rooms.
    candidate.score = (
        roomy_hits * 3.0
        + (10 - min(price_rank, 10))
        + min(len(rows), 12) * 0.4
        + (4.0 if len(fields) > 2 else 0.0)
    )
    return candidate


def _price_digits(raw: str) -> str:
    """A price reduced to the digits two spellings of it have in common.

    "₹1,202.50" from the page and 1202.5 from a payload are the same number
    and share no textual form until the separators and the trailing zeros are
    gone. Only trailing zeros AFTER a decimal point: 3200 must not become 32.
    """
    cleaned = raw.replace(",", "").strip()
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned


def _corroborate(
    candidate: Candidate,
    page_text: str,
    *,
    icon_marked: frozenset[str] = frozenset(),
) -> None:
    """Count how many of the prices actually appear on the page.

    This is the step that separates a finding from a guess. Digits are compared
    without separators, so ₹1,202.50 in the DOM matches 1202.5 in the payload.

    Counted twice, against two different haystacks. ``corroborated`` is the
    original test -- the number is somewhere in the page's text -- and it is
    what a human-reviewed discovery is judged on. ``corroborated_marked`` is
    the same test against only those numbers the page printed a currency
    beside, which is what an unattended repair is judged on. See
    :meth:`Candidate.is_strongly_verified` for why the difference matters.

    ``icon_marked`` carries the second spelling of "printed with a currency
    beside it": the numbers whose currency the page draws as an ICON, which
    leaves nothing in the text for ``_MARKED_PRICE_RE`` to find. Without it a
    page like bookingsmaker's -- every rate in a ``class="fa fa-inr"`` label --
    corroborates fully and marks nothing, so it can be discovered by a person
    and can never be repaired unattended. It is supplied by
    :func:`icon_marked_prices`, which reads it off the DOM; the default empty
    set leaves every text-currency page behaving exactly as before.
    """
    haystack = re.sub(r"[,\s]", "", page_text)
    marked = {
        _price_digits(match.group("digits"))
        for match in _MARKED_PRICE_RE.finditer(page_text or "")
    } | set(icon_marked)

    hits = marked_hits = 0
    for price in candidate.sample_prices:
        whole = str(int(price))
        exact = f"{price.normalize():f}".rstrip("0").rstrip(".")
        if whole in haystack or exact in haystack:
            hits += 1
        if _price_digits(whole) in marked or _price_digits(exact) in marked:
            marked_hits += 1
    candidate.corroborated = hits
    candidate.corroborated_marked = marked_hits


def analyse(
    payloads: list[tuple[str, Any]],
    page_text: str,
    *,
    icon_marked: frozenset[str] = frozenset(),
) -> DiscoveryResult:
    """Score every captured payload and return the best verified candidate.

    Pure: takes what the browser saw and returns a verdict. Kept separate from
    the browser work so it can be tested against recorded payloads.

    ``icon_marked`` is passed straight to :func:`_corroborate` -- see there.
    """
    candidates: list[Candidate] = []
    for url, payload in payloads:
        for path, rows in _iter_arrays(payload):
            found = _evaluate(url, path, rows)
            if found is not None:
                _corroborate(found, page_text, icon_marked=icon_marked)
                # A candidate whose prices are on the page outranks every
                # unverified one, however good its field names look.
                found.score += 25.0 if found.is_verified else 0.0
                candidates.append(found)

    candidates.sort(key=lambda c: c.score, reverse=True)
    notes: list[str] = []
    if not candidates:
        notes.append("No array in any JSON response looked like a room list.")
    elif not candidates[0].is_verified:
        notes.append(
            "Found a plausible room list, but its prices do not appear on the "
            "page. Refusing to trust it: the field may be a deposit, a tax or "
            "a per-person supplement."
        )

    page_prices = re.findall(r"(?:₹|Rs\.?|INR)\s?([\d,]{3,})", page_text)[:12]
    return DiscoveryResult(
        best=candidates[0] if candidates else None,
        others=candidates[1:4],
        page_prices=page_prices,
        notes=notes,
    )



def _has_usable_json(
    payloads: list[tuple[str, Any]],
    page_text: str,
    *,
    icon_marked: frozenset[str] = frozenset(),
) -> bool:
    """Whether the JSON route already produced something corroborated.

    Runs before the browser closes so the DOM scan can still happen if not.
    """
    return analyse(payloads, page_text, icon_marked=icon_marked).ok


#: A number on a booking page is only provably a price when the page writes a
#: currency beside it. Matched against the page's visible text, where the
#: symbol and the digits are frequently in separate elements -- eZee renders
#: "<p>Rs</p><span>3,200.00</span>" -- so anything short and non-numeric is
#: allowed to sit between them.
_MARKED_PRICE_RE = re.compile(
    r"(?:₹|Rs\.?|INR|\$|€|£)[^0-9]{0,3}(?P<digits>[0-9][0-9,]{2,}(?:\.[0-9]{1,2})?)"
)

#: The digits of a price, in the shape ``_MARKED_PRICE_RE`` accepts them. Used
#: on text taken from beside a currency ICON, where the marker has already been
#: established by the class and only the number is left to read.
_PRICE_DIGITS_RE = re.compile(r"[0-9][0-9,]{2,}(?:\.[0-9]{1,2})?")


def why_the_page_cannot_be_learned(
    page_text: str, *, currency_is_an_icon: bool = False
) -> str | None:
    """Why deriving selectors from this page would produce fiction, or None.

    THE FAILURE THIS EXISTS TO STOP
    ===============================
    A real hotel was sold out for the night being checked. All three of its
    rooms rendered "Not Available" and the page carried, in 206 KB of HTML,
    not one price. Auto-repair ran against it anyway, found the "Filter Your
    Search" sidebar, and stored the amenity checkboxes as the room list:

        room_card  div.vres-check-gro
        price      div.vres-chk-box > span
        note       "Auto-repaired: 1 rooms, 1/1 prices confirmed"

    Every guard downstream passed it. With no currency marker anywhere, the
    scan's own ``pageHasMarkedPrices`` test had already switched off and bare
    numbers were admissible, so "Room Size 134 m2" supplied a price;
    corroboration then confirmed that 134 appears on the page, which it does.
    A confident, self-certified, entirely invented configuration -- and it
    replaced a working one, so the hotel could not recover on its own once it
    had rooms to sell again.

    The mistake was not in any single check. It was running discovery at all
    against a page with nothing to discover. A booking page that is showing
    rates always prints at least one of them with a currency beside it; one
    that prints none is either sold out or has not loaded, and in both cases
    the honest answer is "come back later", not a guess that outlives the
    night it was made on.

    Returning a reason rather than a bool because the caller puts it in front
    of a person: "no prices on the page" and "the page says sold out" send an
    operator to different places.
    """
    text = " ".join((page_text or "").split())
    if not text:
        return "the page rendered no text at all"
    # ...unless the page draws its currency as an icon, in which case there is
    # no currency character to find and its absence says nothing. Font
    # Awesome's rupee glyph is common on Indian booking engines: the symbol
    # comes from a CSS ::before rule and the DOM text is bare digits, so a page
    # showing five rates read as a page showing none and was refused.
    if not currency_is_an_icon and not _MARKED_PRICE_RE.search(text):
        # Checked BEFORE the sold-out wording, because it is the stronger
        # signal and the one that holds when a page says nothing at all about
        # why it is empty.
        return (
            "the page does not show a single price with a currency beside it, "
            "so nothing here can be told from a room size or a guest count"
        )
    if looks_sold_out(text):
        return "the page says it has no availability"
    return None


def _candidate_from_dom(card: dict, source_url: str) -> Candidate | None:
    """Turn a DOM scan hit into the same Candidate the JSON route produces."""

    name_selector = str(card.get("name_selector") or "")
    price_selector = str(card.get("price_selector") or "")
    if not name_selector or not price_selector:
        return None
    if name_selector == price_selector:
        # Refused in the scan as well, where a rejected candidate lets the
        # next one be considered. Repeated here because this is the function
        # that turns a scan hit into something writable, and a config naming
        # one element as both the room and its rate has never once been
        # right -- see the identical guard in dom_discovery.py.
        return None

    # The rate plan, when the scan found one. Only a page whose cards were
    # already colliding produces this -- see resolvePlanSelector -- so a
    # healthy source's config is unchanged and its offer keys with it.
    fields = {"room_name": name_selector, "price": price_selector}
    plan_selector = str(card.get("plan_selector") or "")
    if plan_selector and plan_selector not in (name_selector, price_selector):
        fields["meal_plan"] = plan_selector
    else:
        plan_selector = ""

    # Names and plans are paired BEFORE either is filtered. Dropping a blank
    # name from one list and not the other would shift every plan after it by
    # one, and a room would be verified against the plan belonging to the room
    # below it.
    raw_plans = [str(p).strip() for p in (card.get("plans") or [])]
    names, plans = [], []
    for index, value in enumerate(card.get("names") or []):
        name = str(value).strip()
        if not name:
            continue
        names.append(name)
        plans.append(raw_plans[index] if index < len(raw_plans) else "")

    prices: list[Decimal] = []
    for value in card.get("prices") or []:
        parsed = _as_price(value)
        if parsed is not None:
            prices.append(parsed)
    if not names or not prices:
        return None

    return Candidate(
        source_url=source_url,
        rooms_path=card["card"],
        fields=fields,
        kind="dom",
        sample_names=names[:8],
        sample_plans=plans[:8] if plan_selector else [],
        sample_prices=prices[:8],
        room_count=int(card.get("count") or len(names)),
        # Absent on an older scan result: default to trusting, so a missing
        # flag cannot silently reject every DOM candidate.
        name_trusted=bool(card.get("name_trusted", True)),
        # Scored below a JSON find of equal quality: selectors break on a
        # restyle, an API contract usually does not.
        score=20.0 + min(int(card.get("matched") or 0), 10),
    )


#: Text sitting beside a currency icon, read off the DOM. Each icon element
#: contributes its own text and that of the element on either side of it,
#: because the glyph is as often an empty tag next to the digits as it is the
#: tag holding them. Siblings are only read when short, so a card's whole
#: description cannot arrive disguised as a price.
_ICON_MARKED_TEXT_JS = r"""
() => {
  const SEL = '_CURRENCY_ICON_SELECTOR__';
  const out = [];
  const take = (el) => {
    if (!el) return;
    const text = el.textContent || "";
    if (text.length <= 40) out.push(text);
  };
  for (const icon of document.querySelectorAll(SEL)) {
    out.push(icon.textContent || "");
    take(icon.previousElementSibling);
    take(icon.nextElementSibling);
  }
  return out;
}
""".replace("_CURRENCY_ICON_SELECTOR__", CURRENCY_ICON_SELECTOR)


def icon_marked_prices(page) -> frozenset[str]:
    """The numbers this page marks as money with an icon rather than a symbol.

    The counterpart to ``_MARKED_PRICE_RE`` for a page that never writes its
    currency as a character. What ``corroborated_marked`` is really asking is
    "did the page SAY this number is money", and on these pages it said so in a
    class name -- so the answer is read from the DOM instead of the text.

    Both numbers of a discounted pair come back, the struck rack rate included.
    That is correct: this set decides what is money, not which of two prices a
    guest pays. Choosing between them is the scan's job, and it is done by
    :func:`find_room_cards` before a candidate reaches here.
    """
    try:
        chunks = page.evaluate(_ICON_MARKED_TEXT_JS) or []
    except Exception:  # noqa: BLE001 - an unreadable page marks nothing, as before
        return frozenset()
    return frozenset(
        _price_digits(match.group())
        for chunk in chunks
        for match in _PRICE_DIGITS_RE.finditer(chunk or "")
    )


def _page_shows_a_price(page) -> bool:
    """Is this page already displaying rates?

    Both spellings of "a price is on screen": a currency written as text, and
    one drawn as an icon class with the digits bare beside it.
    """
    try:
        if page.query_selector(CURRENCY_ICON_SELECTOR) is not None:
            return True
        return bool(_MARKED_PRICE_RE.search(page.inner_text("body", timeout=5_000) or ""))
    except Exception:  # noqa: BLE001 - a page we cannot read has nothing to protect
        return False


def _open_booking_widget(page) -> None:
    """Click whatever opens the rates, if anything obvious is on the page.

    Deliberately gentle: it tries a short list of labels, takes the first that
    exists, and gives up quietly. This is reconnaissance on a public page, not
    an attempt to drive a checkout — nothing here submits a form, enters
    personal details, or proceeds past the point where rates are shown.
    """
    labels = ("Book Now", "Book now", "Check Availability", "Check availability",
              "View Rates", "Book", "Reserve", "Check Rates")
    for label in labels:
        try:
            control = page.get_by_role("button", name=label).first
            if control.count() == 0:
                continue
            control.click(timeout=6_000)
            log.info("discovery_opened_widget", label=label)
            return
        except Exception:  # noqa: BLE001 - absent or unclickable is expected
            continue

    for label in labels:
        try:
            link = page.get_by_role("link", name=label).first
            if link.count() == 0:
                continue
            link.click(timeout=6_000)
            log.info("discovery_opened_widget", label=label, kind="link")
            return
        except Exception:  # noqa: BLE001
            continue


# ── why a page never opened ──────────────────────────────────────────
#
# Chromium reports every navigation failure through one exception class named
# ``Error``, with the actual reason in its message as a ``net::ERR_*`` code.
# Anything that only looks at the class learns nothing, so the codes are read
# here and turned into the two things the operator actually needs to know:
# whether trying again could help, and whether the address is worth keeping.

#: The connection was accepted and then torn down before any HTML arrived.
#: This is what a bot defence at a CDN edge looks like from the client side --
#: not a challenge page, not a 403, just a closed socket. It is a property of
#: the SITE, so a different URL on the same domain will not behave differently,
#: and no amount of retrying changes it.
_REFUSED_BEFORE_ANY_PAGE = frozenset({
    "ERR_HTTP2_PROTOCOL_ERROR",
    "ERR_QUIC_PROTOCOL_ERROR",
    "ERR_SSL_PROTOCOL_ERROR",
    "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
    "ERR_SSL_CLIENT_AUTH_CERT_NEEDED",
})

#: Ordinary connectivity failures. These say something about the network or
#: the address, and nothing about whether the site would have us.
_NET_ERROR_NOTES = {
    "ERR_NAME_NOT_RESOLVED": "there is no such host, so check the address for a typo",
    "ERR_CONNECTION_REFUSED": "nothing is listening at that address",
    "ERR_CONNECTION_RESET": "the connection dropped part-way through",
    "ERR_CONNECTION_CLOSED": "the connection dropped part-way through",
    "ERR_CONNECTION_TIMED_OUT": "the site did not answer in time",
    "ERR_ADDRESS_UNREACHABLE": "there is no route to that host from here",
    "ERR_INTERNET_DISCONNECTED": "this machine has no network connection",
    "ERR_TOO_MANY_REDIRECTS": "the page redirects in a loop, which is usually a "
                              "login or consent wall",
    "ERR_CERT_AUTHORITY_INVALID": "the site's HTTPS certificate is not trusted",
    "ERR_CERT_DATE_INVALID": "the site's HTTPS certificate has expired",
    "ERR_CERT_COMMON_NAME_INVALID": "the site's HTTPS certificate is for a "
                                    "different host",
    "ERR_EMPTY_RESPONSE": "the site answered with nothing at all",
    "ERR_ABORTED": "the browser abandoned the navigation, which usually means "
                   "the address serves a file download rather than a page",
}

_NET_ERROR_RE = re.compile(r"net::(ERR_[A-Z0-9_]+)")


def _navigation_failure(target: str, exc: Exception):
    """Turn a failed navigation into a sentence somebody can act on.

    The message this replaces was "Could not inspect that page: Error. If it
    showed a CAPTCHA or a bot wall...". Every part of that was wrong for the
    case that produced it: the class name carried no information, and the page
    had shown nothing at all -- there was no CAPTCHA to look for, because the
    site closed the connection before serving a byte.
    """
    from app.core.errors import BlockedError, NetworkError

    message = str(exc)
    match = _NET_ERROR_RE.search(message)
    code = match.group(1) if match else None
    host = urlparse(target).netloc or target[:80]

    if code in _REFUSED_BEFORE_ANY_PAGE:
        # BlockedError, not NetworkError: it is permanent, it must not be
        # retried, and it is the one outcome where the honest advice is to
        # stop asking this site and use another source. Working around it
        # would mean disguising the client, which this system does not do.
        return BlockedError(
            f"{host} closed the connection before sending a page ({code}). "
            f"That is a refusal at the network edge rather than a page we "
            f"could read, and every address on that site behaves the same "
            f"way, so a different link will not help. Track this hotel from "
            f"another site, or by manual entry.",
            context={"url": target, "net_error": code},
        )

    if code and (note := _NET_ERROR_NOTES.get(code)):
        return NetworkError(
            f"Could not reach {host}: {note} ({code}).",
            context={"url": target, "net_error": code},
        )

    # Unknown, so quote Chromium verbatim rather than paraphrasing a failure
    # nobody here anticipated. The first line carries the reason; the rest is
    # a call log that belongs in the artifacts, not in a form field.
    first_line = message.splitlines()[0].strip() if message else type(exc).__name__
    return NetworkError(
        f"Could not open {host}: {first_line}",
        context={"url": target, "net_error": code},
    )


# ── driving the page ─────────────────────────────────────────────────

@contextmanager
def _subprocess_capable_loop_policy() -> Iterator[None]:
    """Let Playwright spawn its Node driver from inside the API process.

    On Windows, uvicorn installs a PROCESS-WIDE
    ``WindowsSelectorEventLoopPolicy`` (uvicorn/loops/asyncio.py).
    ``SelectorEventLoop`` implements no subprocess support whatsoever:
    ``loop.subprocess_exec`` reaches ``_make_subprocess_transport`` and raises
    a bare ``NotImplementedError`` whose message is the empty string.

    Sync Playwright calls ``asyncio.new_event_loop()``, which honours that
    policy, and then tries to launch ``node.exe`` to drive the browser. So
    discovery died with "Could not inspect that page: NotImplementedError" --
    an error with no message, from a page that was never even opened.

    Celery never loads uvicorn, keeps the default Proactor policy, and so
    fetches browsers perfectly well. That asymmetry is why scheduled checks
    worked while the dashboard's Detect button did not.

    Swapping the policy affects only loops created after this point; uvicorn's
    own loop is already running and keeps its selector implementation. The
    previous policy is restored on the way out.
    """
    if sys.platform != "win32":
        yield
        return

    previous = asyncio.get_event_loop_policy()
    if isinstance(previous, asyncio.WindowsProactorEventLoopPolicy):
        yield  # already capable -- e.g. running under Celery
        return

    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)



@contextmanager
def _playwright_for_this_thread() -> Iterator[Any]:
    """A Playwright instance usable from wherever discovery was called.

    There are two callers and they need opposite things.

    **From the API process** there is no pool instance, so one is started here
    and stopped on the way out. It has to be private: the sync API is
    greenlet-based and bound to its creating thread, and the pool is a
    module-level singleton, so borrowing it from a web request handed to a
    threadpool fails with "Cannot switch to a different thread" — which is
    exactly what happened the first time this ran from the dashboard.

    **From a Celery browser worker** the pool has already started one in this
    thread and, by design, never stops it. Starting a second in that state
    raises "It looks like you are using Playwright Sync API inside the asyncio
    loop" — every automatic repair on a worker that had already fetched
    anything, which is all of them after the first. So the live instance is
    borrowed instead, and deliberately NOT stopped: the pool owns it and other
    fetches on this thread are still using it.

    Either way this call launches and closes its OWN browser, so the pool's
    browser is untouched.
    """
    from app.adapters.playwright_base import browser_pool

    existing = browser_pool.current_playwright
    if existing is not None:
        yield existing
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        yield playwright


def _drop_robots_disallowed(
    payloads: list[tuple[str, Any]]
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Keep only the responses we would be allowed to read.

    A RESPONSE WE MAY NOT FETCH IS NOT EVIDENCE WE MAY USE. The page fetches
    these itself, so nothing here was requested by us -- but a candidate built
    from one becomes a stored configuration, and every check from then on reads
    a path the site asked us to stay out of. That is the reasoning already
    written down for Treebo in engines.py, where json_url_contains is
    deliberately absent because "configuring it would read exactly what we have
    been asked not to". It was a rule one profile followed by hand; here it is
    the rule.

    It has to happen BEFORE the JSON is scored, not after. The DOM scan only
    runs when the JSON route found nothing usable, so a disallowed payload that
    wins on merit and is refused afterwards leaves discovery with nothing at
    all -- when the rendered page, which IS allowed, was sitting there the
    whole time. Dropped early, the fallback works as designed.

    Fails open, per RobotsChecker: an unreachable robots.txt is not evidence of
    a prohibition, and refusing on a network blip would turn a transient into a
    hotel nobody can attach.
    """
    if not payloads:
        return payloads, []

    from app.adapters.playwright_base import build_user_agent
    from app.adapters.robots import UNREADABLE_REASON, RobotsChecker
    from app.config import get_settings

    settings = get_settings()
    if not settings.respect_robots_txt:
        return payloads, []

    checker = RobotsChecker(
        build_user_agent(settings.browser_user_agent_suffix), enabled=True
    )
    kept: list[tuple[str, Any]] = []
    refused: list[str] = []
    for url, body in payloads:
        try:
            verdict = checker.check(url)
            # ONLY A REAL PROHIBITION DROPS EVIDENCE.
            #
            # A robots.txt that answers 5xx is read as a blanket disallow, and
            # that is correct when the question is "may we fetch this?" -- a
            # server which cannot state its rules has granted nothing. It is
            # the wrong answer to "may we READ what the page already fetched
            # in front of us?", because the site never said no; its robots
            # host was down.
            #
            # Hotel Golden Nest is the case. commonservice.ipms247.com answers
            # 503 to /robots.txt, so every one of its ten availability
            # endpoints came back disallowed, discovery found nothing, and a
            # working hotel would have been left unrepairable by an outage on
            # a file that has never contained a rule about it.
            refuse = not verdict.allowed and verdict.reason != UNREADABLE_REASON
        except Exception:  # noqa: BLE001 - a failed lookup proves nothing
            refuse = False
        if refuse:
            refused.append(url)
        else:
            kept.append((url, body))

    if refused:
        log.info("discovery_payloads_refused", count=len(refused),
                 first=refused[0][:120])
    return kept, refused


def _note_robots_disallowed_payloads(
    result: DiscoveryResult, payloads: list[tuple[str, Any]], target: str
) -> None:
    """Say so when the prices are somewhere we have been asked not to read.

    A page is allowed while the endpoints it calls are not -- Treebo publishes
    its rates through /api/, and its robots.txt disallows /api/ wholesale.
    Without this, discovery reports only that the prices "do not appear on the
    page", which reads as a parsing problem and sends someone off to find a
    better field path. There is no field path: the answer is a DOM source or
    manual entry, and knowing that immediately saves the search.

    Advisory only. Nothing here blocks anything -- the adapters enforce
    robots.txt at fetch time, which is where refusing belongs.
    """
    if not payloads:
        return

    from app.adapters.playwright_base import build_user_agent
    from app.adapters.robots import UNREADABLE_REASON, RobotsChecker
    from app.config import get_settings

    settings = get_settings()
    if not settings.respect_robots_txt:
        return

    checker = RobotsChecker(build_user_agent(settings.browser_user_agent_suffix), enabled=True)

    considered = {url for url, _ in payloads}
    for candidate in ([result.best] if result.best else []) + list(result.others):
        if getattr(candidate, "source_url", None):
            considered.add(candidate.source_url)

    disallowed: list[str] = []
    for url in considered:
        try:
            verdict = checker.check(url)
            # Only what the site actually said. A 5xx on robots.txt reads as a
            # blanket disallow inside the checker, and printing that as
            # "disallowed by its robots.txt" would put words in the site's
            # mouth on a screen where someone decides whether to trust a
            # hotel's configuration. See _drop_robots_disallowed.
            if not verdict.allowed and verdict.reason != UNREADABLE_REASON:
                disallowed.append(url)
        except Exception:  # noqa: BLE001
            # A robots lookup that fails is not evidence of anything, and this
            # is a note on a screen rather than a decision. Stay quiet.
            continue

    if not disallowed:
        return

    paths = sorted({urlparse(u).path[:60] for u in disallowed})[:4]
    log.info("discovery_robots_disallowed", url=target[:120], count=len(disallowed))
    result.notes.append(
        f"{len(disallowed)} of the JSON endpoint(s) behind this page are "
        f"disallowed by its robots.txt ({', '.join(paths)}). The page itself "
        f"may be readable, but those responses are off-limits, so this hotel "
        f"needs a DOM-based source or manual entry rather than a JSON one."
    )

def inspect_url(
    url: str,
    *,
    check_in: str | None = None,
    check_out: str | None = None,
    adults: int = 2,
    timeout_ms: int = 60_000,
) -> DiscoveryResult:
    """Load a booking page and work out where its prices live.

    Uses the shared browser machinery, so the same politeness rules apply as to
    a real fetch: pinned locale and timezone, resource blocking, an honest
    User-Agent, and a hard stop if a bot wall appears.

    Booking engines load availability after first paint, so the document
    being ready is not the same as the rates being there. This loads, then
    waits for the network to go quiet, then waits a little longer still.

    The quiet is ASKED FOR, not required. A page that never stops fetching
    things is read as it stands rather than abandoned — see the navigation
    block for why that distinction is the difference between supporting an
    OTA and reporting "TimeoutError" about a page that had rendered fine.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    from app.adapters.playwright_base import (
        build_user_agent,
        detect_bot_wall,
    )
    from app.config import get_settings
    from app.core.errors import BlockedError, TimeoutError_

    settings = get_settings()
    target = url
    if check_in and "{check_in" in url:
        # The adapter's own renderer rather than a second implementation of
        # it. A date placeholder may carry the engine's spelling of a date --
        # "{check_in:%d-%m-%Y}" for a site that will not read ISO -- and a
        # plain str.replace of "{check_in}" matches none of those, so the probe
        # fetched a URL with the placeholder still in it and reported, of a
        # page it had never loaded, that it showed no prices.
        from app.adapters.mapping import render_template

        def _as_date(value: str) -> date | str:
            """ISO in, a date out -- so a format spec has something to format.

            Anything else is handed over as written: a caller who passed a
            date already in the engine's own spelling gets it back unchanged,
            which is the behaviour this had before.
            """
            try:
                return date.fromisoformat(value)
            except ValueError:
                return value

        target = render_template(
            url,
            check_in=_as_date(check_in),
            check_out=_as_date(check_out or check_in),
            adults=adults,
            children=0,
            rooms=1,
            nights=1,
        )

    payloads: list[tuple[str, Any]] = []
    dom_cards: list[dict] = []
    unlearnable: str | None = None
    # Set when the page never stopped talking, with how long it was given.
    # Read after the browser is closed, to explain an empty result rather
    # than to cause one.
    never_settled = False
    waited_for_quiet_ms = 0
    # Endpoints the site asked us to stay out of, dropped before scoring.
    refused_by_robots: list[str] = []
    # Declared out here because corroboration below runs after the browser is
    # gone, and a probe that fails before the page is read must still leave a
    # defined -- and empty -- set behind it.
    icon_marked: frozenset[str] = frozenset()

    with _subprocess_capable_loop_policy(), _playwright_for_this_thread() as playwright:
        browser = playwright.chromium.launch(
            headless=settings.browser_headless,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        try:
            ctx = browser.new_context(
                locale=settings.browser_locale,
                timezone_id=settings.browser_timezone,
                viewport={"width": 1366, "height": 900},
                user_agent=build_user_agent(settings.browser_user_agent_suffix),
            )
            page = ctx.new_page()

            def _on_response(response) -> None:
                try:
                    if "application/json" not in response.headers.get("content-type", ""):
                        return
                    if response.status >= 400:
                        return
                    payloads.append((response.url, response.json()))
                except Exception:  # noqa: BLE001 - an unreadable body is not fatal
                    pass

            page.on("response", _on_response)
            # A PAGE IS READY WHEN IT HAS PAINTED ITS RATES, NOT WHEN THE
            # NETWORK GOES QUIET.
            #
            # networkidle means "no requests for 500ms", and a large OTA
            # never offers that: analytics beacons, session heartbeats,
            # lazy-loaded imagery and third-party frames keep traffic moving
            # for as long as the tab is open. Asking for it as the condition
            # of the NAVIGATION meant the whole probe failed on pages that
            # had loaded perfectly -- and failed by machine rather than by
            # site, because whether a page draws breath for half a second is
            # a race between the connection and the trackers. The same URL
            # attached on one laptop and reported "TimeoutError" on another.
            #
            # So: load the document, then ASK for quiet and carry on without
            # it. A page that never settles is inspected anyway -- every JSON
            # response it fetched in the meantime is already captured, which
            # is the thing the wait was ever for.
            try:
                page.goto(target, wait_until="domcontentloaded",
                          timeout=timeout_ms)
            except PlaywrightTimeout as exc:
                # The page genuinely did not load. Said in words,
                # because the caller reports what it catches and
                # "TimeoutError" tells an operator nothing about which
                # of the several waits in here ran out.
                raise TimeoutError_(
                    f"{target[:80]} did not finish loading within "
                    f"{timeout_ms // 1000}s. A slow connection or a site "
                    f"that is down; worth trying again before treating "
                    f"it as a hotel that needs manual entry.",
                    context={"url": target},
                ) from exc
            except PlaywrightError as exc:
                # Navigation failed without ever producing a page. Translated
                # here rather than left to the caller, which sees only the
                # exception CLASS -- and Playwright calls every one of these
                # "Error", so the dashboard reported the string "Error" and
                # then guessed at a CAPTCHA, for a site that had closed the
                # connection before sending a single byte of HTML.
                raise _navigation_failure(target, exc) from exc
            # Never longer than the caller's whole budget: a probe given ten
            # seconds must not spend fifteen of them waiting for quiet.
            settle_ms = min(_SETTLE_MS, timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=settle_ms)
            except PlaywrightTimeout:
                never_settled = True
                waited_for_quiet_ms = settle_ms
                log.info("discovery_page_never_idle", url=target[:120],
                         waited_ms=settle_ms)
            # Widgets frequently fetch rates a beat after the network settles.
            # A page that never settled gets longer: nothing has told us its
            # rates have arrived, so the grace period is all it has.
            page.wait_for_timeout(8_000 if never_settled else 4_000)

            # Many booking widgets fetch nothing until they are opened: the
            # rates arrive on click, not on load. Without this, a site with a
            # perfectly good API reports as exposing none.
            #
            # ONLY when the page is not already showing them. A rates page
            # carries a "Book Room" button per room, and clicking one is how a
            # guest LEAVES that page for the booking form -- so on a page that
            # had already rendered five rates this navigated away from them and
            # then reported, accurately and uselessly, that the page it ended up
            # on had no prices. The click is for pages that hide their rates
            # behind a button, and a page displaying them plainly is exactly the
            # page that must not be touched.
            if not payloads and not _page_shows_a_price(page):
                _open_booking_widget(page)
                page.wait_for_timeout(5_000)

            if marker := detect_bot_wall(page):
                raise BlockedError(
                    f"{target} shows a bot wall ({marker!r}). Stopping; this "
                    f"site needs a human decision, not a workaround.",
                    context={"url": target, "marker": marker},
                )
            # THE OTHER WAIT THAT USED TO END THE PROBE THE SAME WAY.
            #
            # Reading the body of a heavy OTA page is not always a
            # five-second job, and when it was not, this raised a bare
            # Playwright TimeoutError from three lines below a bot-wall
            # check -- indistinguishable, to whoever read the message, from
            # the navigation timing out. One more try with a longer budget,
            # by which point the page has had several seconds more to
            # finish laying itself out.
            #
            # No fallback to page.content(): corroboration is the rule that
            # makes discovery trustworthy, and it asks whether a price is on
            # SCREEN. Matching against raw HTML would confirm prices out of
            # scripts and hidden markup -- a config that looks verified and
            # is not. Better to fail, and say so.
            try:
                page_text = page.inner_text("body", timeout=5_000) or ""
            except PlaywrightTimeout:
                try:
                    page_text = page.inner_text("body", timeout=15_000) or ""
                except PlaywrightTimeout as exc:
                    raise TimeoutError_(
                        f"Loaded {target[:80]} but could not read the text of "
                        f"the page within 20s -- it is unusually heavy, or "
                        f"still rendering. Worth one more attempt; if it "
                        f"keeps happening this hotel needs manual entry.",
                        context={"url": target},
                    ) from exc

            # JSON first, because an API contract survives a restyle and CSS
            # does not. The DOM scan is the fallback for pages that render
            # their prices server-side -- common among small independent
            # hotels, and the case that made this necessary.
            # A page with no rates on it cannot teach anything about where
            # its rates live, and the scan is not built to notice that -- it
            # is built to find repetition, and an empty booking page is still
            # full of repetition. See why_the_page_cannot_be_learned.
            # Asked of the DOM, because a currency painted by CSS leaves
            # nothing in the text for the guard to find.
            try:
                currency_is_an_icon = page.query_selector(CURRENCY_ICON_SELECTOR) is not None
            except Exception:  # noqa: BLE001 - a selector failure must not end the probe
                currency_is_an_icon = False
            unlearnable = why_the_page_cannot_be_learned(
                page_text, currency_is_an_icon=currency_is_an_icon
            )

            # Collected while the browser is still open: corroboration runs
            # after it closes, and by then the only record of which numbers
            # the page called money is this set.
            # Guarded like the query_selector above it, and for the reason:
            # a DOM read that fails is a lost hint, not a lost probe.
            try:
                icon_marked = (
                    icon_marked_prices(page) if currency_is_an_icon else frozenset()
                )
            except Exception:  # noqa: BLE001 - see above
                icon_marked = frozenset()

            # Before the JSON is scored, and so before the decision about
            # whether the DOM is needed: a payload we may not read must not
            # win, and must not suppress the fallback that would have.
            payloads, refused_by_robots = _drop_robots_disallowed(payloads)

            if not _has_usable_json(payloads, page_text, icon_marked=icon_marked):
                if unlearnable:
                    log.info("dom_scan_skipped", url=target[:120], why=unlearnable)
                else:
                    from app.adapters.dom_discovery import find_room_cards

                    dom_cards = find_room_cards(page)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - shutdown must not mask the result
                pass

    result = analyse(payloads, page_text, icon_marked=icon_marked)

    if not result.ok and dom_cards:
        # Every candidate, not just the best-ranked one. The ranking is built
        # from repetition and card size, which are good hints and are not the
        # test -- the test is corroboration. Taking only the first meant a
        # page whose top candidate failed to corroborate was reported as
        # unreadable while the candidate directly behind it was correct.
        dom_candidate = None
        for card in dom_cards:
            considered = _candidate_from_dom(card, target)
            if considered is None:
                continue
            _corroborate(considered, page_text, icon_marked=icon_marked)
            if considered.is_verified:
                dom_candidate = considered
                break

        if dom_candidate is not None:
            # A verified DOM finding beats an unverified JSON one: the whole
            # point of corroboration is that confirmed beats plausible,
            # whatever it was read from.
            if result.best is not None:
                result.others.insert(0, result.best)
            result.best = dom_candidate
            result.notes.append(
                f"No usable JSON, so the rendered page was scanned: found "
                f"{dom_candidate.room_count} room cards."
            )
        else:
            result.notes.append(
                f"Scanned the rendered page too: none of its "
                f"{len(dom_cards)} candidate room lists had prices that could "
                f"be confirmed against what the page displays."
            )

    if not result.ok and never_settled:
        result.notes.append(
            f"The page never stopped fetching things, so it was read after "
            f"{waited_for_quiet_ms // 1000}s rather than when it went quiet. If its "
            f"rates arrive later than that, the URL you land on AFTER "
            f"picking dates will have them on it already."
        )

    if not result.ok and unlearnable:
        result.unlearnable = unlearnable
        result.notes.append(
            f"Did not read selectors off the rendered page: {unlearnable}. "
            f"Nothing was changed. Try again when the page is showing rates — "
            f"a configuration derived from an empty page would outlive the "
            f"night that emptied it."
        )

    if refused_by_robots:
        paths = sorted({urlparse(u).path[:60] for u in refused_by_robots})[:4]
        result.notes.append(
            f"{len(refused_by_robots)} JSON response(s) were left out of this "
            f"reading because the site's robots.txt disallows them "
            f"({', '.join(paths)}). A configuration built on one would read a "
            f"path we have been asked to stay out of, on every check. The "
            f"rendered page is the permitted surface here."
        )

    _note_robots_disallowed_payloads(result, payloads, target)

    result.notes.insert(
        0,
        f"Inspected {len(payloads)} JSON response(s)"
        + (f" and {len(dom_cards)} DOM candidate(s)" if dom_cards else "")
        + f" from {target[:80]}",
    )
    log.info(
        "discovery_complete",
        url=target[:120],
        payloads=len(payloads),
        verified=result.ok,
        rooms=result.best.room_count if result.best else 0,
    )
    return result
