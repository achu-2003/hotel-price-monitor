"""The fast path: a booking engine that answers in JSON.

When the source probe (``scripts/probe_site.py``) finds an availability
endpoint, promote that hotel here. This adapter is ~50x cheaper than driving a
browser — about 300ms and 5MB against 25s and 400MB — and, more importantly,
JSON survives a site redesign while CSS selectors do not.

Everything about the endpoint lives in ``hotel_sources.adapter_config``:

.. code-block:: yaml

    endpoint: "https://book.example.com/api/availability"
    method: GET                      # or POST
    query:
      checkin: "{check_in}"
      checkout: "{check_out}"
      adults: "{adults}"
    rooms_path: "data.roomTypes"
    fields:
      room_name: "name"
      price_inclusive: "rates.0.totalInclusive"
      meal_plan: "rates.0.boardCode"
      available: "isAvailable"
    sold_out_when_empty: true

The same politeness rules apply as to the browser: robots.txt first, honest
User-Agent, no evasion.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.mapping import (
    booking_conditions,
    dig,
    offer_from_mapping,
    render_template,
)
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

log = get_logger("adapter.http_json")

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


class HttpJsonAdapter:
    """Reads prices from a source's own JSON availability endpoint."""

    adapter_key = "http_json"
    queue = "http"

    def fetch(self, context: FetchContext) -> FetchResult:
        settings = get_settings()
        config = context.config or {}

        endpoint = config.get("endpoint") or context.url
        if not endpoint:
            raise AdapterConfigError(
                "http_json needs an 'endpoint' in adapter_config, or a url on "
                "the hotel_source row."
            )

        url = render_template(
            endpoint,
            check_in=context.check_in,
            check_out=context.check_out,
            nights=context.stay.nights,
            adults=context.adults,
            children=context.children,
            rooms=context.rooms,
            currency=context.currency,
            external_id=context.external_id or "",
        )
        params = {
            key: render_template(
                str(value),
                check_in=context.check_in,
                check_out=context.check_out,
                nights=context.stay.nights,
                adults=context.adults,
                children=context.children,
                rooms=context.rooms,
                currency=context.currency,
                external_id=context.external_id or "",
            )
            for key, value in (config.get("query") or {}).items()
        }

        user_agent = build_user_agent(settings.browser_user_agent_suffix)
        RobotsChecker(
            user_agent,
            cache=_cache_or_none(),
            enabled=settings.respect_robots_txt,
        ).assert_allowed(url)

        started = time.monotonic()
        payload = self._request(
            url,
            method=str(config.get("method", "GET")).upper(),
            params=params,
            json_body=config.get("body"),
            headers={"User-Agent": user_agent, "Accept": "application/json",
                     "Accept-Language": context.locale},
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        offers, sold_out = self._parse(payload, config, context)
        return FetchResult(
            offers=offers,
            sold_out_detected=sold_out,
            duration_ms=duration_ms,
            source_url=url,
        )

    # -- transport ---------------------------------------------------
    def _request(
        self,
        url: str,
        *,
        method: str,
        params: dict[str, str],
        json_body: Any,
        headers: dict[str, str],
    ) -> Any:
        """One request, with every failure mapped onto the error taxonomy.

        The classification matters more than the request: a 429 must halve our
        budget rather than trigger three fast retries, and a 403 must stop us
        entirely rather than being treated as a transient blip.
        """
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = client.request(
                    method, url, params=params or None, json=json_body, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise TimeoutError_(f"Timed out calling {url}", context={"url": url}) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc), context={"url": url}) from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimitedError(
                f"{url} rate limited us",
                retry_after_seconds=int(retry_after) if (retry_after or "").isdigit() else None,
                context={"url": url},
            )
        if status in (401, 407):
            raise AuthError(f"{url} requires authentication", context={"url": url})
        if status == 403:
            # Not retried, by design. A 403 on an endpoint is the machine-
            # readable version of "you are not welcome here".
            raise BlockedError(
                f"{url} returned 403. Treating this as a refusal and stopping; "
                f"this source needs a human decision, not a workaround.",
                context={"url": url},
            )
        if status >= 500:
            raise HttpStatusError(f"{url} returned {status}", status_code=status,
                                  context={"url": url})
        if status >= 400:
            raise SchemaDriftError(
                f"{url} returned {status}. The endpoint or its parameters have "
                f"probably changed.",
                context={"url": url, "status_code": status},
            )

        try:
            return response.json()
        except ValueError as exc:
            raise SchemaDriftError(
                f"{url} did not return JSON (content-type "
                f"{response.headers.get('content-type')!r}).",
                context={"url": url, "body_head": response.text[:300]},
            ) from exc

    # -- shaping -----------------------------------------------------
    def _parse(
        self, payload: Any, config: dict, context: FetchContext
    ) -> tuple[list[NormalizedOffer], bool]:
        mapping = config.get("fields") or {}
        if not mapping.get("room_name"):
            raise AdapterConfigError(
                "adapter_config.fields.room_name is required: without it an "
                "offer cannot be matched to a room type."
            )

        nodes = dig(payload, config.get("rooms_path"), None)
        if nodes is None:
            raise SchemaDriftError(
                f"rooms_path {config.get('rooms_path')!r} resolved to nothing. "
                f"The endpoint's shape has changed.",
                context={"keys": list(payload)[:20] if isinstance(payload, dict) else None},
            )
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
        if not isinstance(nodes, list):
            raise SchemaDriftError(
                f"rooms_path {config.get('rooms_path')!r} resolved to "
                f"{type(nodes).__name__}, expected a list of rooms."
            )

        if not nodes:
            # Ambiguity resolved by configuration rather than by guessing: only
            # a hotel we have confirmed returns [] when sold out gets to call
            # an empty list "sold out".
            if config.get("sold_out_when_empty"):
                return [], True
            raise SchemaDriftError(
                "The endpoint returned no rooms and this source is not "
                "configured to read that as sold out. Refusing to guess — set "
                "sold_out_when_empty once the behaviour is confirmed.",
            )

        offers: list[NormalizedOffer] = []
        for node in nodes:
            offer = offer_from_mapping(node, mapping, default_currency=context.currency,
                                       params=booking_conditions(context))
            offers.append(offer)

        sold_out = bool(offers) and not any(o.is_available for o in offers)
        return offers, sold_out


def _cache_or_none():
    """Redis for the robots cache, or ``None`` if it is unreachable.

    The robots checker treats a missing cache as "fetch every time", which is
    slower but never less correct.
    """
    try:
        return get_redis()
    except Exception as exc:  # noqa: BLE001
        log.warning("robots_cache_unavailable", error=str(exc))
        return None
