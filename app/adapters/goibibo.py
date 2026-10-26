"""Goibibo hotel pages, read from the room list the page itself fetches.

WHY A BROWSER, AND WHY THIS ONE
===============================
Goibibo (MakeMyTrip's, on the same edge) has no API a hotel can be read from
and refuses the scraper's browser at the door. Probed on 26 Oct 2026 against
AGS Holiday Resorts, three nights each, a fresh context per night:

    bundled Chromium, headless                net::ERR_HTTP2_PROTOCOL_ERROR
    installed Chrome, headless                200, empty room list
    installed Chrome, window, our UA/blocking 200, empty room list
    installed Chrome, window, as a visitor    200, every room, 3 of 3 nights

So this adapter opens the page ``as_visitor`` (see BrowserPool.context). The
decision to read Goibibo that way was the property owner's, made for their
own listing; it is recorded here so nobody has to rediscover it.

The same probe showed the other half: the edge scores FREQUENCY. After about
thirty loads in a few minutes even the visitor browser was handed empty pages.
Hence the low rate limit on the engine profile, and why an empty page is
:class:`RateLimitedError` -- back off -- and never a sold-out night or a
redesign.

WHY CODE AND NOT A FIELD MAPPING
================================
The prices are in ``mapi.goibibo.com/.../search-rooms``, and the one that is
shown is chosen BY KEY:

    tariffs[].defaultPriceKey = "GOSMARTSAVING"
    tariffs[].priceMap = {"GOICICIFEST": {...}, "GOSMARTSAVING": {...}, ...}

Each entry is a coupon. The bank ones (ICICI card, ICICI EMI) are cheaper and
are not what a guest is shown; the default key is. A dotted path cannot say
"the entry named by a sibling field", and reading the first or the cheapest
would record a bank-card price as the public one. Same reasoning as aiosell
and hotelzify.

WHICH NUMBER
============
``PRICE_AFTER_DISCOUNT`` is the headline on the card ("₹5,214 + ₹600 taxes &
service fees") and is per night, pre-tax. ``BASE_FARE`` is the struck rate
beside it and is never recorded as the price -- the same trap the Agoda and
Booking.com profiles document.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.mapping import dedupe_offers
from app.adapters.playwright_base import BrowserFetch, build_user_agent, open_page
from app.adapters.playwright_direct_site import _cache_or_none
from app.adapters.playwright_ota import PlaywrightOtaAdapter
from app.adapters.robots import RobotsChecker
from app.config import get_settings
from app.core.errors import RateLimitedError, SchemaDriftError
from app.core.logging import get_logger
from app.services import meal_plan

log = get_logger("adapter.goibibo")

#: The page's own request for the room list.
SEARCH_ROOMS = "/cg/search-rooms/"

#: How long the room list is waited for. It arrives two to four seconds after
#: the document on a good load; a throttled load never sends it at all.
_WAIT_MS = 25_000

#: How long a throttled source is left alone. The edge's memory is minutes,
#: not seconds -- on the probe, a pause of a few seconds between loads did
#: nothing.
_BACK_OFF_SECONDS = 900

#: Goibibo's meal-plan codes, as the three boards the repricing rule compares
#: on. Anything else passes through as the code itself rather than a guess:
#: the meal plan is part of the offer key.
_BOARDS = {
    "EP": meal_plan.ROOM_ONLY,
    "CP": meal_plan.BREAKFAST,
    "MAP": meal_plan.HALF_BOARD,
}

_SOLD_OUT_MARKERS = ("sold out on goibibo", "no rooms available")

#: Everything a throttled visitor is shown, seen on 26 Oct 2026.
_REFUSAL_BODY = "200 - OK"


class GoibiboAdapter(PlaywrightOtaAdapter):
    """A Goibibo hotel-details page, read as a visitor."""

    adapter_key = "goibibo"
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

        log.info("ota_fetch", hotel=context.hotel_name,
                 hotel_source_id=context.hotel_source_id,
                 check_in=context.check_in.isoformat())
        started = time.monotonic()
        with open_page(url, locale=context.locale, timezone=context.timezone,
                       artifact_label=f"hs{context.hotel_source_id}",
                       as_visitor=True) as fetch:
            payload = _wait_for_rooms(fetch, int(config.get("wait_timeout_ms", _WAIT_MS)))
            says_sold_out = _page_says_sold_out(fetch)

            offers = offers_from_search_rooms(payload, context) if payload is not None else []
            if not offers:
                if not says_sold_out:
                    # Neither rooms nor "sold out": the throttled page. Raised
                    # inside the page so its screenshot is kept.
                    raise RateLimitedError(
                        f"Goibibo sent no room list for {context.hotel_name} on "
                        f"{context.check_in.isoformat()}. That is how it slows a "
                        f"visitor down, not a sold-out night; backing off.",
                        retry_after_seconds=_BACK_OFF_SECONDS,
                        context={"url": url, "room_list_seen": payload is not None},
                    )
                log.info("goibibo_sold_out", hotel=context.hotel_name,
                         check_in=context.check_in.isoformat())

        return FetchResult(
            offers=offers,
            sold_out_detected=not offers,
            duration_ms=int((time.monotonic() - started) * 1000),
            source_url=url,
        )


def _wait_for_rooms(fetch: BrowserFetch, timeout_ms: int) -> Any | None:
    """The search-rooms payload, or None if the page never asked for it."""
    deadline = time.monotonic() + timeout_ms / 1000
    checked_body_at = 0.0
    while time.monotonic() < deadline:
        payload = fetch.find_json(SEARCH_ROOMS)
        if payload is not None:
            return payload
        # The refusal is a page whose whole text is "200 - OK" -- no app, so
        # no room list is ever coming, and waiting the budget out for it only
        # keeps a browser slot from the next hotel.
        now = time.monotonic()
        if now - checked_body_at >= 1.0:
            checked_body_at = now
            if _body_text(fetch).strip() == _REFUSAL_BODY:
                log.info("goibibo_edge_refused")
                return None
        fetch.page.wait_for_timeout(250)
    log.info("goibibo_room_list_never_arrived")
    return None


def _page_says_sold_out(fetch: BrowserFetch) -> bool:
    body = _body_text(fetch).lower()
    return any(marker in body for marker in _SOLD_OUT_MARKERS)


def _body_text(fetch: BrowserFetch) -> str:
    try:
        return fetch.page.inner_text("body")
    except Exception:  # noqa: BLE001 - a page mid-navigation has no body
        return ""


def offers_from_search_rooms(payload: Any, context: FetchContext) -> list[NormalizedOffer]:
    """Every room and rate plan on the page, at the price the page shows.

    One offer per room, board and cancellation terms; where Goibibo lists two
    rate plans that agree on all three, the cheaper is kept, as the card does.
    """
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        raise SchemaDriftError(
            "Goibibo's search-rooms answer has no 'response' object; the page "
            "has changed how it delivers rooms.",
            context={"keys": sorted(payload)[:20] if isinstance(payload, dict) else None},
        )

    offers: list[NormalizedOffer] = []
    priced = 0
    for room in response.get("exactRooms") or []:
        name = (room.get("roomName") or "").strip()
        if not name:
            continue
        for plan in room.get("ratePlans") or []:
            for tariff in plan.get("tariffs") or []:
                if _shown_amounts(tariff) is None:
                    continue
                priced += 1
                offer = _offer(room, plan, tariff, name, context)
                if offer is not None:
                    offers.append(offer)

    # Drift is "no tariff had a price we can read", not "none was for this
    # party": a room priced only for three adults is an answer, just not ours.
    if response.get("exactRooms") and not priced:
        raise SchemaDriftError(
            "Goibibo listed rooms but none carried a readable price under its "
            "defaultPriceKey; the price layout has changed.",
            context={"rooms": len(response["exactRooms"])},
        )
    return dedupe_offers(offers, "cheapest")


def _offer(room: dict, plan: dict, tariff: dict, name: str,
           context: FetchContext) -> NormalizedOffer | None:
    # A tariff priced for a different party than the one asked for is a
    # different question's answer. Goibibo prices the search occupancy, but
    # says so per tariff, so it is checked rather than assumed.
    occupancy = tariff.get("occupancydetails") or {}
    adults = occupancy.get("numberOfAdults")
    if adults is not None and int(adults) != context.adults:
        return None

    amounts = _shown_amounts(tariff)
    if amounts is None:
        return None
    price = amounts["PRICE_AFTER_DISCOUNT"]
    taxes = amounts.get("TAXES")

    code = (plan.get("mealPlanCode") or "").upper()
    terms = ((plan.get("cancellationPolicy") or {}).get("type") or "").upper()
    rooms_left = tariff.get("availCount")
    return NormalizedOffer(
        raw_room_name=name,
        price_exclusive=price,
        taxes_fees=taxes,
        price_inclusive=price + taxes if taxes is not None else None,
        currency=context.currency,
        meal_plan=_BOARDS.get(code, code or None),
        # FC is free cancellation, NR non-refundable; anything else unstated.
        refundable={"FC": True, "NR": False}.get(terms),
        rooms_left=int(rooms_left) if isinstance(rooms_left, (int, float)) else None,
        # Goibibo's room code survives a rename; the name is marketing copy.
        site_room_id=str(room["roomCode"]) if room.get("roomCode") else None,
        raw_payload={"room": name, "rate_plan": plan.get("name"), "meal_plan": code,
                     "price_key": tariff.get("defaultPriceKey"), "amounts": {k: str(v) for k, v in amounts.items()}},
    )


def _shown_amounts(tariff: dict) -> dict[str, Decimal | None] | None:
    """The price breakdown under the tariff's own default key, if it has one."""
    key = tariff.get("defaultPriceKey")
    entry = (tariff.get("priceMap") or {}).get(key) if key else None
    if not isinstance(entry, dict):
        return None
    amounts = {d.get("key"): _money(d.get("amount")) for d in entry.get("details") or []
               if isinstance(d, dict)}
    price = amounts.get("PRICE_AFTER_DISCOUNT")
    return amounts if price is not None and price > 0 else None


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
