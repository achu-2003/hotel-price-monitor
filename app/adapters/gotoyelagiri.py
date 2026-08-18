"""gotoyelagiri.com — a local portal carrying most of Yelagiri in one call.

WHY THIS ONE MATTERS MORE THAN THE OTHERS
=========================================
Aiosell and eZee are per-property: each hotel is its own fetch. This endpoint
returns **every resort on the portal in a single response** — 50-odd rooms
across ~20 properties, unauthenticated, in about a second. For a competitor set
concentrated in one hill station, that is close to total coverage from one
integration.

So a hotel here is not an integration at all. It is a filter: set
``external_id`` to the portal's resort id and this returns that property's
rooms out of the shared payload.

THE SHARED RESPONSE IS CACHED, ON PURPOSE
=========================================
Twenty hotels each pulling the same ~95KB every 30 minutes is 2MB a cycle
aimed at one small server, to answer a question that has one answer. The
payload is cached for a couple of minutes so a dispatch sweep costs the portal
ONE request rather than twenty. That is politeness, and it is also why this
adapter can be pointed at every property on the portal without hesitation.

WHAT THIS SOURCE CANNOT TELL YOU
================================
Its prices do not vary by date. The same rates come back for tonight, for two
days out and for six weeks out — the portal publishes a standing rate rather
than per-night pricing, which was confirmed by asking it for three different
stays and getting identical numbers.

That is recorded honestly rather than papered over: every offer carries
``standing_rate: True`` in its payload, so a series built from this source is
never mistaken for one tracking real nightly movement. A change here means the
hotel changed its published rate, which is a genuine and useful signal — just a
slower one than a revenue-managed property produces.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import httpx

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.playwright_base import build_user_agent
from app.config import get_settings
from app.core.errors import (
    AdapterConfigError,
    BlockedError,
    HttpStatusError,
    NetworkError,
    RateLimitedError,
    SchemaDriftError,
    TimeoutError_,
)
from app.core.logging import get_logger
from app.core.ratelimit import get_redis

log = get_logger("adapter.gotoyelagiri")

_ENDPOINT = "https://api.gotoyelagiri.com/api/resort/room/new"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

#: Long enough that one dispatch sweep shares a single response; short enough
#: that a manual "Run now" a few minutes later sees fresh data.
_CACHE_KEY = "gotoyelagiri:rooms"
_CACHE_TTL_SECONDS = 150


class GotoYelagiriAdapter:
    """Reads one resort's rooms out of the portal's shared payload."""

    adapter_key = "gotoyelagiri"
    queue = "http"

    def fetch(self, context: FetchContext) -> FetchResult:
        resort_id = self._resort_id(context)

        started = time.monotonic()
        rooms = self._all_rooms()
        mine = [
            room for room in rooms
            if isinstance(room, dict) and str(room.get("resort")) == resort_id
        ]

        if not mine:
            # The portal answered, but this property was not in it. Delisted,
            # renumbered, or the id is wrong — all of which need a human, and
            # none of which should be recorded as "sold out".
            available = sorted({
                f"{r.get('resort')}={r.get('resortName')}"
                for r in rooms if isinstance(r, dict)
            })
            raise SchemaDriftError(
                f"Resort id {resort_id!r} is not in the portal's response. It may "
                f"have been delisted or renumbered.",
                context={"known_resorts": available[:25]},
            )

        offers = [self._to_offer(room, context) for room in mine]
        offers = [o for o in offers if o is not None]
        if not offers:
            raise SchemaDriftError(
                f"{len(mine)} rooms found for resort {resort_id} but none carried "
                f"a usable price. The payload shape has probably changed.",
                context={"sample": [str(r.get('name'))[:40] for r in mine[:5]]},
            )

        return FetchResult(
            offers=offers,
            sold_out_detected=not any(o.is_available for o in offers),
            duration_ms=int((time.monotonic() - started) * 1000),
            source_url=_ENDPOINT,
        )

    # -- inputs ------------------------------------------------------
    def _resort_id(self, context: FetchContext) -> str:
        if context.external_id and str(context.external_id).strip():
            return str(context.external_id).strip()
        configured = (context.config or {}).get("resort_id")
        if configured:
            return str(configured).strip()
        raise AdapterConfigError(
            "gotoyelagiri needs the portal's resort id in external_id. Run "
            "scripts/list_yelagiri.py to see every resort and its id."
        )

    # -- transport ---------------------------------------------------
    def _all_rooms(self) -> list[dict]:
        """The portal's full room list, shared across every hotel on it."""
        cached = self._from_cache()
        if cached is not None:
            return cached

        settings = get_settings()
        headers = {
            "User-Agent": build_user_agent(settings.browser_user_agent_suffix),
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = client.get(_ENDPOINT, headers=headers)
        except httpx.TimeoutException as exc:
            raise TimeoutError_("Timed out calling gotoyelagiri",
                                context={"url": _ENDPOINT}) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc), context={"url": _ENDPOINT}) from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimitedError(
                "gotoyelagiri rate limited us",
                retry_after_seconds=int(retry_after) if (retry_after or "").isdigit() else None,
            )
        if status == 403:
            raise BlockedError(
                "gotoyelagiri returned 403. Treating this as a refusal and "
                "stopping; this source needs a human decision, not a workaround.",
                context={"url": _ENDPOINT},
            )
        if status >= 500:
            raise HttpStatusError(f"gotoyelagiri returned {status}",
                                  status_code=status, context={"url": _ENDPOINT})
        if status >= 400:
            raise SchemaDriftError(f"gotoyelagiri returned {status}.",
                                   context={"url": _ENDPOINT})

        try:
            payload = response.json()
        except ValueError as exc:
            raise SchemaDriftError(
                "gotoyelagiri did not return JSON.",
                context={"body_head": response.text[:300]},
            ) from exc

        rooms = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rooms, list):
            raise SchemaDriftError(
                f"Expected a list of rooms, got {type(rooms).__name__}."
            )

        self._to_cache(rooms)
        log.info("gotoyelagiri_fetched", rooms=len(rooms),
                 resorts=len({r.get("resort") for r in rooms if isinstance(r, dict)}))
        return rooms

    def _from_cache(self) -> list[dict] | None:
        try:
            raw = get_redis().get(_CACHE_KEY)
        except Exception as exc:  # noqa: BLE001 - a cache miss is never fatal
            log.warning("gotoyelagiri_cache_read_failed", error=str(exc))
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _to_cache(self, rooms: list[dict]) -> None:
        try:
            get_redis().setex(_CACHE_KEY, _CACHE_TTL_SECONDS, json.dumps(rooms))
        except Exception as exc:  # noqa: BLE001
            log.warning("gotoyelagiri_cache_write_failed", error=str(exc))

    # -- shaping -----------------------------------------------------
    def _to_offer(self, room: dict, context: FetchContext) -> NormalizedOffer | None:
        name = str(room.get("name") or "").strip()
        price = _to_decimal(room.get("price"))
        if not name or price is None:
            return None

        left = room.get("available_rooms")
        try:
            rooms_left = int(left) if left is not None else None
        except (TypeError, ValueError):
            rooms_left = None

        # isAvailable is absent on the shared listing and present on the
        # per-resort one. Absent is treated as available: the portal lists it,
        # and inventing a sell-out would be a change nobody made.
        flag = room.get("isAvailable")
        is_available = True if flag is None else bool(flag)
        if rooms_left == 0:
            is_available = False

        return NormalizedOffer(
            raw_room_name=name,
            price_inclusive=price,
            currency=context.currency or "INR",
            meal_plan=None,
            refundable=None,
            is_available=is_available,
            rooms_left=rooms_left,
            raw_payload={
                "resort": room.get("resort"),
                "resort_name": room.get("resortName"),
                "original_price": room.get("originalPrice"),
                "discount": room.get("discount"),
                "adults": room.get("noofAdults"),
                "children": room.get("noofchild"),
                # Stated on every offer so a series from this source is never
                # mistaken for one tracking genuine per-night movement.
                "standing_rate": True,
            },
        )


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    # Zero or negative is a data artefact, never a free room.
    return amount if amount > 0 else None
