"""Sending tonight's prices to Sales Daddy after a check of our own hotel.

``price_feed.push`` is queued by every fetch of an own property for tonight
(tasks_fetch._ingest). It reads the whole list -- every room on every
platform, not only the one just checked -- and PUTs it to Sales Daddy's
partner API when it differs from what was last sent. Sales Daddy upserts each
item on ``source`` + ``external_id``, so a send replaces the last one.

The whole list each time, so one platform's check also re-sends the other
two at their last known prices: a send that went missing is repaired by the
next one rather than by bookkeeping per item.
"""
from __future__ import annotations

from typing import Any

import httpx
import redis
from celery import shared_task

from app.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import get_redis
from app.db.models import Hotel
from app.db.session import sync_session
from app.services import price_feed
from app.services.dates import local_today

log = get_logger("tasks.price_feed")

_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class Retryable(Exception):
    """Sales Daddy was down or busy; the same list is worth sending again."""


def _sent_key(hotel_id: int) -> str:
    return f"price_feed:sent:{hotel_id}"


def _already_sent(hotel_id: int, mark: str) -> bool:
    try:
        sent = get_redis().get(_sent_key(hotel_id))
    except redis.RedisError:
        return False  # sending twice is harmless; not sending is not
    return sent is not None and sent.decode() == mark


def _remember(hotel_id: int, mark: str) -> None:
    try:
        get_redis().set(_sent_key(hotel_id), mark, ex=get_settings().price_feed_resend_hours * 3600)
    except redis.RedisError as exc:
        log.warning("price_feed_not_remembered", hotel_id=hotel_id, error=str(exc))


def enabled() -> bool:
    return get_settings().salesdaddy_partner_api_key is not None


@shared_task(
    name="price_feed.push", ignore_result=True,
    autoretry_for=(Retryable, httpx.TransportError),
    retry_backoff=30, retry_backoff_max=600, max_retries=5,
)
def push_prices(hotel_id: int) -> dict[str, Any]:
    settings = get_settings()
    if settings.salesdaddy_partner_api_key is None:
        return {"status": "off"}
    night = local_today(settings.timezone)
    with sync_session() as session:
        hotel = session.get(Hotel, hotel_id)
        if hotel is None or not hotel.is_own_property:
            return {"status": "not_own"}
        rows = price_feed.to_rows(session.execute(price_feed.rows_stmt(hotel_id, night)).all())
        body = price_feed.payload(hotel, night, price_feed.items(hotel_id, rows))
    if not body["items"]:
        return {"status": "empty"}
    mark = price_feed.fingerprint(body)
    if _already_sent(hotel_id, mark):
        return {"status": "unchanged"}

    url = settings.salesdaddy_base_url.rstrip("/") + settings.salesdaddy_products_path
    response = httpx.put(
        url, json=body, timeout=_TIMEOUT,
        headers={"X-Api-Key": settings.salesdaddy_partner_api_key.get_secret_value()},
    )
    if response.status_code == 429 or response.status_code >= 500:
        log.warning("price_feed_retry", hotel_id=hotel_id, status=response.status_code)
        raise Retryable(f"Sales Daddy answered {response.status_code}")
    if response.status_code >= 400:
        # A refused key or a body they do not accept fails the same way every
        # time; retrying would only repeat it. The next change tries again.
        log.error("price_feed_refused", hotel_id=hotel_id, status=response.status_code,
                  body=response.text[:500])
        return {"status": "refused", "code": response.status_code}
    _remember(hotel_id, mark)
    log.info("price_feed_sent", hotel_id=hotel_id, items=len(body["items"]))
    return {"status": "sent", "items": len(body["items"])}
