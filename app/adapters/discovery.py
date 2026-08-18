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

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

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
    """One possible room list, with the paths needed to read it."""

    source_url: str
    rooms_path: str
    fields: dict[str, str]
    sample_names: list[str] = field(default_factory=list)
    sample_prices: list[Decimal] = field(default_factory=list)
    room_count: int = 0
    #: How many of the prices were also found in the page's visible text.
    corroborated: int = 0
    score: float = 0.0

    @property
    def is_verified(self) -> bool:
        """At least half the prices appear on the page a guest sees.

        Half rather than all: a page often shows only the cheapest rate per
        room, or hides sold-out rooms, so demanding every price would reject
        good candidates. Demanding none would accept invented ones.
        """
        return bool(self.sample_prices) and self.corroborated * 2 >= len(self.sample_prices)

    def as_adapter_config(self, json_fragment: str) -> dict[str, Any]:
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
    # A private Playwright instance, NOT the shared browser_pool.
    #
    # The sync API is greenlet-based and bound to the thread that created it.
    # The pool is a module-level singleton, so reusing it from the API's worker
    # thread fails with "Cannot switch to a different thread" — which is
    # exactly what happened the first time this ran from the dashboard. Celery
    # gets away with the pool because its solo worker is one thread; a web
    # request handed to a threadpool is not.
    #
    # Discovery runs once per hotel, so a browser per call is the right trade:
    # a second of startup against a whole class of threading bug.
    from playwright.sync_api import sync_playwright

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

    with sync_playwright() as playwright:
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
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - shutdown must not mask the result
                pass

    result = analyse(payloads, page_text)
    result.notes.insert(
        0, f"Inspected {len(payloads)} JSON response(s) from {target[:90]}"
    )
    log.info(
        "discovery_complete",
        url=target[:120],
        payloads=len(payloads),
        verified=result.ok,
        rooms=result.best.room_count if result.best else 0,
    )
    return result
