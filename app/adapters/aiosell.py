"""Aiosell booking engine -- a first-class adapter.

Aiosell (``be.aiosell.com`` / ``live.aiosell.com``) is a widely used Indian
hotel booking engine. It exposes the same rates endpoint its own booking page
consumes, which makes it the best kind of source: no browser, ~1s per fetch,
and a shape that survives redesigns of the customer-facing page entirely.

WHICH ENDPOINT, AND WHY IT MATTERS
==================================
Aiosell publishes two, and picking the wrong one silently records a wrong price:

``rates-inventory``       the RACK rate -- the struck-through number on the page
``booking-engine-rates``  the SELLABLE rate -- what a guest actually pays  <-- this one

For the first property tested, the rack rate was 1,850 while the real price was
1,202.50: a 35% promotion. Both are "the price" in some sense, and only one is
the number a competitor decision should rest on. We record what the guest is
charged, and keep the rack rate in ``raw_payload`` so a promotion starting or
ending is diagnosable afterwards -- otherwise a discount expiring looks
identical to the hotel raising its rate.

The endpoint also accepts ``noOfAdults`` / ``noOfKids``, so the SERVER resolves
occupancy pricing. That is worth more than it looks: the alternative is picking
a rate out of a per-occupancy list ourselves and being quietly wrong whenever a
room prices a 2-adult stay differently from how we assumed.

WHY THIS IS NOT `http_json` + CONFIG
====================================
The payload is keyed by room slug, then by meal-plan code, with the price
several levels down and availability on a sibling branch::

    { "standard-room": { "available": true, "totalCount": 6,
                         "displayName": "Standard Room",
                         "rates": { "EP": { "total_rate_tax_inclusive": 1202.5,
                                            "total_tax": 60.125,
                                            "original_rate": 1850 } } } }

Two things the generic config language deliberately cannot express: a room name
that lives in a key, and iterating a dict of meal plans to emit one offer each.
Rather than growing a query language nobody can debug at 2 AM, this is explicit
Python that says what it means and is testable against a recorded payload.
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.playwright_base import build_user_agent
from app.adapters.robots import RobotsChecker
from app.config import get_settings
from app.core.errors import (
    AdapterConfigError,
    AuthError,
    BlockedError,
    HttpStatusError,
    NetworkError,
    RateLimitedError,
    SchemaDriftError,
    TimeoutError_,
)
from app.core.logging import get_logger
from app.core.ratelimit import get_redis

log = get_logger("adapter.aiosell")

_API_ROOT = "https://live.aiosell.com/api/v1/rms/hotels"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

#: Standard hotel meal-plan codes, expanded to something a person reading an
#: alert will understand. Only a fallback: Aiosell publishes its own wording
#: per property, which is preferred. An unknown code passes through untouched
#: rather than being guessed at, because meal plan is part of the offer key and
#: inventing a label would split one price series into two.
_MEAL_PLANS = {
    "EP": "Room Only",
    "CP": "Breakfast",
    "AP": "All Meals",
    "MAP": "Breakfast + Dinner",
    "BB": "Breakfast",
    "HB": "Half Board",
    "FB": "Full Board",
    "AI": "All Inclusive",
}

#: The booking URL carries the property's code: /book/<code>
_CODE_FROM_URL = re.compile(r"/book/([A-Za-z0-9_-]+)")


class AiosellAdapter:
    """Reads sellable rates from Aiosell's own booking-engine API."""

    adapter_key = "aiosell"
    queue = "http"

    def fetch(self, context: FetchContext) -> FetchResult:
        settings = get_settings()
        hotel_code = self._hotel_code(context)

        url = f"{_API_ROOT}/{hotel_code}/booking-engine-rates"
        # start/end are the NIGHTS stayed, so the last night is check-out minus
        # one day. Passing check_out directly would price an extra night.
        last_night = context.check_out - timedelta(days=1)
        params = {
            "start": context.check_in.isoformat(),
            "end": max(last_night, context.check_in).isoformat(),
            "noOfRooms": str(context.rooms),
            "noOfAdults": str(context.adults),
            "noOfKids": str(context.children),
            "source": "client",
        }

        user_agent = build_user_agent(settings.browser_user_agent_suffix)
        # The same politeness rules as the browser path: robots.txt is checked
        # against the API host before anything is requested from it.
        RobotsChecker(
            user_agent, cache=_cache_or_none(), enabled=settings.respect_robots_txt
        ).assert_allowed(url)

        started = time.monotonic()
        payload = self._request(url, params, user_agent)
        offers = self._to_offers(payload, context)

        return FetchResult(
            offers=offers,
            # Rooms returned but none sellable is a genuine sell-out. Distinct
            # from an empty response, which is treated as drift below.
            sold_out_detected=bool(offers) and not any(o.is_available for o in offers),
            duration_ms=int((time.monotonic() - started) * 1000),
            source_url=f"{url}?start={params['start']}&end={params['end']}",
        )

    # -- inputs ------------------------------------------------------
    def _hotel_code(self, context: FetchContext) -> str:
        """The property's Aiosell code.

        Taken from ``external_id`` when set, otherwise lifted out of the
        booking URL, so attaching a hotel needs only the URL an operator can
        copy from their address bar.
        """
        if context.external_id:
            return context.external_id.strip()

        explicit = (context.config or {}).get("hotel_code")
        if explicit:
            return str(explicit).strip()

        if context.url and (match := _CODE_FROM_URL.search(context.url)):
            return match.group(1)

        raise AdapterConfigError(
            "Aiosell needs the property code. Set external_id on the hotel "
            "source, or give a booking URL of the form "
            "https://be.aiosell.com/book/<code>."
        )

    # -- transport ---------------------------------------------------
    def _request(self, url: str, params: dict[str, str], user_agent: str) -> Any:
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = client.get(
                    url,
                    params=params,
                    headers={"User-Agent": user_agent, "Accept": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise TimeoutError_(f"Timed out calling {url}", context={"url": url}) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc), context={"url": url}) from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimitedError(
                "Aiosell rate limited us",
                retry_after_seconds=int(retry_after) if (retry_after or "").isdigit() else None,
                context={"url": url},
            )
        if status in (401, 403):
            # 403 on an API is the machine-readable form of "not welcome".
            raise (AuthError if status == 401 else BlockedError)(
                f"Aiosell returned {status} for {url}", context={"url": url}
            )
        if status >= 500:
            raise HttpStatusError(
                f"Aiosell returned {status}", status_code=status, context={"url": url}
            )
        if status >= 400:
            raise SchemaDriftError(
                f"Aiosell returned {status}. The property code or the endpoint "
                f"has probably changed.",
                context={"url": url, "status_code": status},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SchemaDriftError(
                "Aiosell did not return JSON.", context={"body_head": response.text[:300]}
            ) from exc

    # -- shaping -----------------------------------------------------
    def _to_offers(self, payload: Any, context: FetchContext) -> list[NormalizedOffer]:
        """One offer per (room, meal plan).

        Meal plans stay separate rather than collapsing to the cheapest: a
        room-only rate and a breakfast-inclusive rate are different products,
        and merging them would make a guest switching plans look like a price
        change.
        """
        if not isinstance(payload, dict):
            raise SchemaDriftError(
                f"Expected an object keyed by room slug, got {type(payload).__name__}."
            )
        if not payload:
            # Genuinely ambiguous: no rooms at all could be a closed-out date or
            # a changed endpoint. Refusing to guess means no price is written,
            # which is the correct outcome for an unexplained empty response.
            raise SchemaDriftError(
                "Aiosell returned no rooms. Either the property is closed for "
                "these dates or the endpoint has changed; refusing to record "
                "this as a sell-out without evidence."
            )

        offers: list[NormalizedOffer] = []
        for slug, node in payload.items():
            if isinstance(node, dict):
                offers.extend(self._offers_for_room(slug, node, context))

        if not offers:
            raise SchemaDriftError(
                f"{len(payload)} rooms returned but none carried a usable rate "
                f"for {context.adults} adult(s). The rate structure has probably "
                f"changed.",
                context={"rooms": list(payload)[:10]},
            )
        return offers

    def _offers_for_room(
        self, slug: str, node: dict, context: FetchContext
    ) -> list[NormalizedOffer]:
        rates = node.get("rates")
        if not isinstance(rates, dict) or not rates:
            # A room with no rates for this party size is simply not offered.
            return []

        # ``available`` is the engine's own flag; totalCount is the inventory
        # behind it. Either being falsy means it cannot be booked.
        count = _to_int(node.get("totalCount"))
        is_available = bool(node.get("available", True)) and (count is None or count > 0)

        display_name = (
            str(node.get("displayName") or node.get("name") or "").strip()
            or _prettify(slug)
        )
        plan_labels = self._plan_labels(node)

        offers: list[NormalizedOffer] = []
        for plan_code, entry in rates.items():
            if not isinstance(entry, dict) or entry.get("isActive") is False:
                continue

            # What the guest is charged, tax included -- the number printed on
            # the booking page. Several equivalent fields are tried because
            # Aiosell populates them slightly differently per property.
            inclusive = _to_decimal(
                _first_present(
                    entry.get("total_rate_tax_inclusive"),
                    entry.get("total_rate"),
                    _first_night(entry, "sellRateTaxInclusive", "sellRate"),
                )
            )
            if inclusive is None:
                continue

            taxes = _to_decimal(entry.get("total_tax"))
            exclusive = inclusive - taxes if taxes is not None else None

            code = str(plan_code).strip().upper()
            offers.append(
                NormalizedOffer(
                    raw_room_name=display_name,
                    price_inclusive=inclusive,
                    price_exclusive=exclusive,
                    taxes_fees=taxes,
                    currency=context.currency or "INR",
                    meal_plan=plan_labels.get(code) or _MEAL_PLANS.get(code, code or None),
                    refundable=None,  # not published on this endpoint
                    is_available=is_available,
                    rooms_left=count,
                    raw_payload={
                        "slug": slug,
                        "mealplan": code,
                        # The struck-through rack rate. Kept so a promotion
                        # starting or ending stays diagnosable: without it, a
                        # discount expiring looks exactly like a rate rise.
                        "original_rate": entry.get("original_rate"),
                        "room_discount": entry.get("roomDiscount"),
                        "online_pay_discount": entry.get("onlinePayDiscount"),
                        "total_count": node.get("totalCount"),
                        "adults": context.adults,
                    },
                )
            )
        return offers

    def _plan_labels(self, node: dict) -> dict[str, str]:
        """Aiosell's own wording per meal plan, e.g. EP becomes "Rooms Only".

        Preferred over the generic table so an alert reads the way the hotel's
        own page does.
        """
        labels: dict[str, str] = {}
        for plan in node.get("rateplans") or []:
            if not isinstance(plan, dict):
                continue
            code = str(plan.get("mealplan") or "").strip().upper()
            label = str(plan.get("displayName") or "").strip()
            if code and label and code not in labels:
                labels[code] = label
        return labels


def _prettify(slug: str) -> str:
    """``standard-room`` becomes ``Standard Room``.

    Only a fallback for a payload with no display name. Room matching
    normalises the result again, so capitalisation is cosmetic.
    """
    cleaned = re.sub(r"[-_]+", " ", str(slug)).strip()
    return " ".join(word.capitalize() for word in cleaned.split()) or str(slug)


def _first_present(*values: Any) -> Any:
    """First value that is neither None nor zero.

    Zero is excluded deliberately: Aiosell zero-fills fields it does not
    populate for a property, and a zero rate is a data artefact rather than a
    free room.
    """
    for value in values:
        if value is not None and value != 0:
            return value
    return None


def _first_night(entry: dict, *fields: str) -> Any:
    """Per-night figures live under ``prices``; take the first night's."""
    prices = entry.get("prices")
    if isinstance(prices, list) and prices and isinstance(prices[0], dict):
        for field in fields:
            if prices[0].get(field) is not None:
                return prices[0][field]
    return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    """Rates arrive as numbers; anything unparseable is dropped, not guessed."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    # A zero or negative rate is a data artefact, never a free room.
    return amount if amount > 0 else None


def _cache_or_none():
    try:
        return get_redis()
    except Exception as exc:  # noqa: BLE001
        log.warning("robots_cache_unavailable", error=str(exc))
        return None
