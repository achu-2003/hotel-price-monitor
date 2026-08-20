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
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from app.adapters.parsing import MAX_PLAUSIBLE_PRICE, MIN_PLAUSIBLE_PRICE
from app.core.logging import get_logger

log = get_logger("discovery")

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
    sample_prices: list[Decimal] = field(default_factory=list)
    room_count: int = 0
    #: How many of the prices were also found in the page's visible text.
    corroborated: int = 0
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
        """
        if not self.sample_prices or self.corroborated * 2 < len(self.sample_prices):
            return False
        if (
            not self.name_trusted
            and len(self.sample_names) > 1
            and len(set(self.sample_names)) == 1
        ):
            return False
        return True

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


def _corroborate(candidate: Candidate, page_text: str) -> None:
    """Count how many of the prices actually appear on the page.

    This is the step that separates a finding from a guess. Digits are compared
    without separators, so ₹1,202.50 in the DOM matches 1202.5 in the payload.
    """
    haystack = re.sub(r"[,\s]", "", page_text)
    hits = 0
    for price in candidate.sample_prices:
        whole = str(int(price))
        exact = f"{price.normalize():f}".rstrip("0").rstrip(".")
        if whole in haystack or exact in haystack:
            hits += 1
    candidate.corroborated = hits


def analyse(
    payloads: list[tuple[str, Any]], page_text: str
) -> DiscoveryResult:
    """Score every captured payload and return the best verified candidate.

    Pure: takes what the browser saw and returns a verdict. Kept separate from
    the browser work so it can be tested against recorded payloads.
    """
    candidates: list[Candidate] = []
    for url, payload in payloads:
        for path, rows in _iter_arrays(payload):
            found = _evaluate(url, path, rows)
            if found is not None:
                _corroborate(found, page_text)
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



def _has_usable_json(payloads: list[tuple[str, Any]], page_text: str) -> bool:
    """Whether the JSON route already produced something corroborated.

    Runs before the browser closes so the DOM scan can still happen if not.
    """
    return analyse(payloads, page_text).ok


def _candidate_from_dom(card: dict, source_url: str) -> Candidate | None:
    """Turn a DOM scan hit into the same Candidate the JSON route produces."""

    names = [str(n).strip() for n in (card.get("names") or []) if str(n).strip()]
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
        fields={
            "room_name": card["name_selector"],
            "price": card["price_selector"],
        },
        kind="dom",
        sample_names=names[:8],
        sample_prices=prices[:8],
        room_count=int(card.get("count") or len(names)),
        # Absent on an older scan result: default to trusting, so a missing
        # flag cannot silently reject every DOM candidate.
        name_trusted=bool(card.get("name_trusted", True)),
        # Scored below a JSON find of equal quality: selectors break on a
        # restyle, an API contract usually does not.
        score=20.0 + min(int(card.get("matched") or 0), 10),
    )


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

    from app.adapters.robots import RobotsChecker
    from app.adapters.playwright_base import build_user_agent
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
            if not checker.check(url).allowed:
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

    Booking engines load availability after first paint, so this waits for the
    network to settle and then a little longer — returning at DOMContentLoaded
    would inspect an empty page and report that the site exposes nothing.
    """
    from app.adapters.playwright_base import (
        build_user_agent,
        detect_bot_wall,
    )
    from app.config import get_settings
    from app.core.errors import BlockedError

    settings = get_settings()
    target = url
    if check_in and "{check_in}" in url:
        target = (
            url.replace("{check_in}", check_in)
            .replace("{check_out}", check_out or check_in)
            .replace("{adults}", str(adults))
            .replace("{children}", "0")
            .replace("{rooms}", "1")
        )

    payloads: list[tuple[str, Any]] = []
    dom_cards: list[dict] = []

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
            page.goto(target, wait_until="networkidle", timeout=timeout_ms)
            # Widgets frequently fetch rates a beat after the network settles.
            page.wait_for_timeout(4_000)

            # Many booking widgets fetch nothing until they are opened: the
            # rates arrive on click, not on load. Without this, a site with a
            # perfectly good API reports as exposing none.
            if not payloads:
                _open_booking_widget(page)
                page.wait_for_timeout(5_000)

            if marker := detect_bot_wall(page):
                raise BlockedError(
                    f"{target} shows a bot wall ({marker!r}). Stopping; this "
                    f"site needs a human decision, not a workaround.",
                    context={"url": target, "marker": marker},
                )
            page_text = page.inner_text("body", timeout=5_000) or ""

            # JSON first, because an API contract survives a restyle and CSS
            # does not. The DOM scan is the fallback for pages that render
            # their prices server-side -- common among small independent
            # hotels, and the case that made this necessary.
            if not _has_usable_json(payloads, page_text):
                from app.adapters.dom_discovery import find_room_cards

                dom_cards = find_room_cards(page)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - shutdown must not mask the result
                pass

    result = analyse(payloads, page_text)

    if not result.ok and dom_cards:
        dom_candidate = _candidate_from_dom(dom_cards[0], target)
        if dom_candidate is not None:
            _corroborate(dom_candidate, page_text)
            if dom_candidate.is_verified:
                # A verified DOM finding beats an unverified JSON one: the
                # whole point of corroboration is that confirmed beats
                # plausible, whatever it was read from.
                if result.best is not None:
                    result.others.insert(0, result.best)
                result.best = dom_candidate
                result.notes.append(
                    f"No usable JSON, so the rendered page was scanned: found "
                    f"{dom_candidate.room_count} room cards."
                )
            else:
                result.notes.append(
                    "Scanned the rendered page too, but its prices could not be "
                    "confirmed against what the page displays."
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
