"""The primary adapter: a competitor's own booking engine, driven by Chromium.

This is where most of the project's value comes from. A hotel's own site names
its rooms the way the hotel names them, states the meal plan, and says whether
a rate is refundable — the three things an aggregator flattens away.

Two extraction strategies, tried in this order:

1. **The booking engine's own JSON**, captured from the network while the page
   loads. Configure ``json_url_contains`` and the adapter uses the response the
   page itself fetched. This is the outcome to aim for: it survives redesigns.
2. **The DOM**, via per-hotel selectors. Works everywhere, breaks whenever the
   site is restyled — which is why a break raises ``SchemaDriftError`` with a
   screenshot rather than writing a guessed number.

All of it is configuration on ``hotel_sources.adapter_config``:

.. code-block:: yaml

    url_template: "https://book.example.com/search?in={check_in}&out={check_out}&ad={adults}"
    json_url_contains: ["/api/availability"]     # strategy 1
    rooms_path: "data.rooms"
    fields: {room_name: "name", price_inclusive: "rate.total"}

    wait_for: ".room-card"                        # strategy 2
    room_card: ".room-card"
    selectors:
      room_name: ".room-card__title"
      price: ".room-card__price"
      meal_plan: ".room-card__board"
      sold_out: ".room-card--unavailable"
    sold_out_markers: ["No rooms available for these dates"]
"""
from __future__ import annotations

import time

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.mapping import dig, offer_from_mapping, render_template
from app.adapters.parsing import (
    card_looks_sold_out,
    declared_tax_basis,
    detect_currency,
    looks_sold_out,
    parse_price,
    parse_price_or_none,
    parse_rooms_left,
)
from app.adapters.playwright_base import BrowserFetch, build_user_agent, open_page
from app.adapters.robots import RobotsChecker
from app.config import get_settings
from app.core.errors import AdapterConfigError, SchemaDriftError
from app.core.logging import get_logger
from app.core.ratelimit import get_redis

log = get_logger("adapter.direct_site")

#: How long to wait for the room list after the page reports domcontentloaded.
#: Booking engines fetch availability after first paint, so waiting for the
#: rooms selector is what actually decides whether we saw the prices.
_ROOMS_WAIT_MS = 25_000

#: How long to keep waiting AFTER the room container appears, for a price to
#: render inside it. Separate from the container budget on purpose: the two
#: waits answer different questions, and a sold-out page pays this one in full
#: only when it publishes no sold-out marker to short-circuit it.
_PRICE_WAIT_MS = 15_000


class PlaywrightDirectSiteAdapter:
    """Reads prices from a hotel's own booking engine."""

    adapter_key = "playwright_direct_site"
    queue = "browser"

    def fetch(self, context: FetchContext) -> FetchResult:
        settings = get_settings()
        config = context.config or {}

        url = self._resolve_url(context, config)

        RobotsChecker(
            build_user_agent(settings.browser_user_agent_suffix),
            cache=_cache_or_none(),
            enabled=settings.respect_robots_txt,
        ).assert_allowed(url)

        started = time.monotonic()
        label = f"hs{context.hotel_source_id}"

        with open_page(
            url,
            locale=context.locale,
            timezone=context.timezone,
            artifact_label=label,
        ) as fetch:
            self._wait_for_rooms(fetch, config)

            offers, sold_out = self._extract_json(fetch, config, context)
            if offers is None:
                offers, sold_out = self._extract_dom(fetch, config, context)

        return FetchResult(
            offers=offers,
            sold_out_detected=sold_out,
            duration_ms=int((time.monotonic() - started) * 1000),
            source_url=url,
        )

    # -- setup -------------------------------------------------------
    def _resolve_url(self, context: FetchContext, config: dict) -> str:
        template = config.get("url_template") or context.url
        if not template:
            raise AdapterConfigError(
                "playwright_direct_site needs a url on the hotel_source row or "
                "a url_template in adapter_config."
            )
        return render_template(
            template,
            check_in=context.check_in,
            check_out=context.check_out,
            nights=context.stay.nights,
            adults=context.adults,
            children=context.children,
            rooms=context.rooms,
            currency=context.currency,
            external_id=context.external_id or "",
        )

    def _wait_for_rooms(self, fetch: BrowserFetch, config: dict) -> None:
        """Wait for whatever this source actually delivers the prices in.

        A timeout is NOT raised here: the page may legitimately be showing "no
        rooms available", which the extraction step is better placed to judge.
        Raising would report a sold-out weekend as a broken adapter.
        """
        timeout_ms = int(config.get("wait_timeout_ms", _ROOMS_WAIT_MS))

        # When the prices come from an XHR, wait for THAT, not for a DOM node.
        # Booking engines fetch availability after first paint, so returning as
        # soon as the document is parsed would look at an empty page and report
        # drift on a site that was working perfectly.
        fragments = config.get("json_url_contains")
        if fragments:
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                if fetch.find_json(*fragments) is not None:
                    return
                fetch.page.wait_for_timeout(250)
            log.info("availability_json_never_arrived", fragments=fragments)
            return

        selector = config.get("wait_for") or config.get("room_card")
        if not selector:
            return
        try:
            fetch.page.wait_for_selector(selector, timeout=timeout_ms)
        except PlaywrightTimeout:
            log.info("rooms_selector_never_appeared", selector=selector)
            return

        self._wait_for_a_price(fetch, config)

    def _wait_for_a_price(self, fetch: BrowserFetch, config: dict) -> None:
        """Wait for the rooms to be PRICED, not merely present.

        The room container and the prices inside it do not arrive together. One
        real source waits on ``#t-roomTypes`` — a section that exists as soon as
        the page renders, while its rates come from an XHR a second or two
        later. Reading the moment the container appeared found the card, found
        no price in it, and raised SchemaDriftError: "1 room cards matched but
        none yielded a price. The price selector is stale."

        The selector was not stale. It read the price perfectly on the next
        check, and on eight of the eleven around it. What the alert actually
        reported was that we looked too early — and it reported it as a site
        redesign, which is the one thing that makes a person go and rewrite a
        working config.

        Waiting for EITHER a price or a sold-out marker, rather than just a
        price, is what keeps this cheap. A genuinely sold-out night never
        renders a price, and blocking the full budget on every one of those
        would trade a false alarm for a slow check.

        Never raises. A page that shows neither is left to the extraction step,
        which is better placed to tell "sold out" from "redesigned" and already
        does.
        """
        selectors = config.get("selectors") or {}
        price_selector = selectors.get("price")
        if not price_selector:
            return

        budget_ms = int(config.get("price_wait_ms", _PRICE_WAIT_MS))
        markers = [m.lower() for m in (config.get("sold_out_markers") or [])]
        deadline = time.monotonic() + budget_ms / 1000
        checked_body_at = 0.0

        while time.monotonic() < deadline:
            try:
                if fetch.page.query_selector(price_selector) is not None:
                    return
            except PlaywrightError:
                # A selector the page cannot evaluate is drift, not a timing
                # problem, and saying so is the extraction step's job.
                return

            # Reading the whole body is far dearer than a selector lookup, so
            # the sold-out check runs about once a second rather than on every
            # poll.
            now = time.monotonic()
            if markers and now - checked_body_at >= 1.0:
                checked_body_at = now
                body = _safe_text(fetch.page, "body").lower()
                if any(marker in body for marker in markers):
                    return

            fetch.page.wait_for_timeout(250)

        log.info("price_never_rendered", selector=price_selector, waited_ms=budget_ms)

    # -- strategy 1: the page's own JSON -----------------------------
    def _extract_json(
        self, fetch: BrowserFetch, config: dict, context: FetchContext
    ) -> tuple[list[NormalizedOffer] | None, bool]:
        """Returns ``(None, False)`` when this strategy is not configured or the
        response was not seen, so the caller falls through to the DOM."""
        fragments = config.get("json_url_contains")
        if not fragments:
            return None, False

        payload = fetch.find_json(*fragments)
        if payload is None:
            log.info("availability_json_not_seen", fragments=fragments)
            return None, False

        mapping = config.get("fields") or {}
        if not mapping.get("room_name"):
            raise AdapterConfigError(
                "json_url_contains is set but fields.room_name is missing; the "
                "captured payload cannot be mapped to offers."
            )

        nodes = dig(payload, config.get("rooms_path"), None)
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
        if not isinstance(nodes, list):
            log.info("json_rooms_path_missed", path=config.get("rooms_path"))
            return None, False
        if not nodes:
            return ([], True) if config.get("sold_out_when_empty") else (None, False)

        offers = [
            offer_from_mapping(node, mapping, default_currency=context.currency)
            for node in nodes
        ]
        log.info("offers_from_json", count=len(offers), hotel=context.hotel_name)
        return offers, bool(offers) and not any(o.is_available for o in offers)

    # -- strategy 2: the DOM -----------------------------------------
    def _extract_dom(
        self, fetch: BrowserFetch, config: dict, context: FetchContext
    ) -> tuple[list[NormalizedOffer], bool]:
        selectors = config.get("selectors") or {}
        card_selector = config.get("room_card")
        if not card_selector or not selectors.get("room_name") or not selectors.get("price"):
            raise AdapterConfigError(
                "DOM extraction needs room_card plus selectors.room_name and "
                "selectors.price in adapter_config. Run scripts/probe_site.py "
                "against this hotel to find them."
            )

        page = fetch.page
        cards = page.query_selector_all(card_selector)

        if not cards:
            # Nothing found. The page either says "sold out" or it has been
            # redesigned, and the difference decides whether we write a
            # business event or raise an alert. We never guess between them.
            body = _safe_text(page, "body")[:4000]
            markers = config.get("sold_out_markers") or []
            if any(m.lower() in body.lower() for m in markers) or looks_sold_out(body):
                log.info("sold_out_detected", hotel=context.hotel_name)
                return [], True
            raise SchemaDriftError(
                f"No elements matched room_card selector {card_selector!r} and "
                f"the page does not say it is sold out. This is almost "
                f"certainly a redesign — see the saved screenshot.",
                context={"selector": card_selector, "hotel": context.hotel_name},
            )

        offers: list[NormalizedOffer] = []
        reasons: list[str] = []
        for card in cards:
            try:
                offers.append(self._offer_from_card(card, selectors, context))
            except SchemaDriftError as exc:
                # One malformed card must not discard the other four rooms that
                # parsed cleanly. The gap is visible; a lost page load is not.
                reasons.append(str(exc))
                log.warning("room_card_skipped", reason=str(exc), hotel=context.hotel_name)

        if not offers:
            # Report the selector that ACTUALLY failed.
            #
            # This message used to blame the price selector unconditionally.
            # When every card had failed on its NAME instead -- which is what
            # happens when the name selector cannot resolve inside a card at
            # all -- it accused a price selector that was reading the page
            # perfectly, and sent whoever read the alert to the one part of the
            # config that was not broken.
            blamed = (
                "room_name" if all("carried no name" in r for r in reasons) else "price"
            )
            raise SchemaDriftError(
                f"{len(cards)} room cards matched but none could be read. "
                f"The {blamed} selector {selectors.get(blamed)!r} is stale: "
                f"{reasons[0] if reasons else 'no card produced a usable offer'}.",
                context={
                    "cards": len(cards),
                    "hotel": context.hotel_name,
                    "failing_selector": blamed,
                    "reasons": reasons[:3],
                },
            )

        log.info("offers_from_dom", count=len(offers), hotel=context.hotel_name)
        return offers, not any(o.is_available for o in offers)

    def _offer_from_card(self, card, selectors: dict, context: FetchContext) -> NormalizedOffer:
        name = _text_in(card, selectors["room_name"])
        if not name:
            raise SchemaDriftError("Room card carried no name")

        sold_out_selector = selectors.get("sold_out")
        card_text = _element_text(card)
        sold_out = bool(
            (sold_out_selector and card.query_selector(sold_out_selector) is not None)
            or looks_sold_out(card_text)
        )

        price_text = _text_in(card, selectors["price"])
        price = None
        if not sold_out:
            # A room with no readable price is one of two things, and the
            # difference is the difference between a business fact and a bug.
            #
            # Until now it was always read as the bug. A hotel with all three
            # of its rooms showing "Not Available" -- no price on the page
            # anywhere, nothing to read and nothing wrong -- reported "none
            # yielded a price, the price selector is stale", which is a false
            # accusation against a selector that was working, and it repeated
            # every half hour for as long as the hotel stayed full.
            #
            # The card gets asked before it is condemned. Only a card that
            # produced no price is asked, so a room that says "Breakfast not
            # available" beside a rate is unaffected -- see
            # card_looks_sold_out for why that ordering is the safeguard.
            #
            # parse_price, not parse_price_or_none: the non-raising variant
            # drops the lower bound to zero, which would turn the "1" out of
            # "1 extra-large double bed" into a one-rupee room instead of the
            # out-of-range refusal that catches a selector on the wrong
            # element.
            try:
                price = parse_price(price_text, field_name=f"price of {name[:40]!r}")
            except SchemaDriftError:
                if not card_looks_sold_out(card_text):
                    # A listed room with an unreadable price and no
                    # explanation is drift, and a guessed number is worse
                    # than a gap.
                    raise
                sold_out = True

        is_available = not sold_out

        exclusive_text = _text_in(card, selectors.get("price_exclusive"))
        taxes_text = _text_in(card, selectors.get("taxes_fees"))
        exclusive = parse_price_or_none(exclusive_text, field_name="price_exclusive")
        taxes = parse_price_or_none(taxes_text, field_name="taxes_fees")

        # One scraped number has to be filed as one component or the other, and
        # the card usually says which: "Room Rates Exclusive of Tax Rs 3,200".
        # Filing a stated pre-tax rate as the all-in price is harmless while it
        # is the only figure we hold -- the offer falls back to whichever
        # exists -- but it stops being harmless the moment a tax line is also
        # captured, because the pre-tax rate then gets the tax subtracted from
        # it a second time. Believing the page costs nothing and closes that.
        inclusive = price
        if price is not None and exclusive is None:
            if declared_tax_basis(card_text) == "exclusive":
                exclusive, inclusive = price, None

        return NormalizedOffer(
            raw_room_name=name,
            price_inclusive=inclusive,
            price_exclusive=exclusive,
            taxes_fees=taxes,
            currency=detect_currency(price_text or "", default=context.currency),
            meal_plan=_text_in(card, selectors.get("meal_plan")),
            refundable=_refundable(card, selectors),
            is_available=is_available,
            rooms_left=parse_rooms_left(_text_in(card, selectors.get("rooms_left"))),
            raw_payload={"card_text": card_text[:1000]},
        )


def _refundable(card, selectors: dict) -> bool | None:
    """Tri-state on purpose.

    "Unknown refundability" and "non-refundable" are different offers and must
    not collide in the offer key, so an absent selector stays ``None``.
    """
    selector = selectors.get("refundable")
    if not selector:
        return None
    text = _text_in(card, selector)
    if text is None:
        return None
    lowered = text.lower()
    if "non-refundable" in lowered or "non refundable" in lowered:
        return False
    if "refundable" in lowered or "free cancel" in lowered:
        return True
    return None


def _text_in(element, selector: str | None) -> str | None:
    if not selector:
        return None
    try:
        child = element.query_selector(selector)
    except PlaywrightError:
        return None
    if child is None:
        return None
    return _element_text(child) or None


def _element_text(element) -> str:
    try:
        return " ".join((element.inner_text() or "").split())
    except PlaywrightError:
        return ""


def _safe_text(page, selector: str) -> str:
    try:
        return page.inner_text(selector, timeout=2_000) or ""
    except PlaywrightError:
        return ""


def _cache_or_none():
    try:
        return get_redis()
    except Exception as exc:  # noqa: BLE001
        log.warning("robots_cache_unavailable", error=str(exc))
        return None
