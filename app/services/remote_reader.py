"""Checks handed to a reader on another connection, and what it sends back.

Goibibo scores the caller's ADDRESS. From 16 Nov 2026 it served the production
server's data-centre one nothing but its "Sold Out" page, while the same
adapter on a home connection read every room in the same minute. No code on
the server changes the address it calls from, so the page is read somewhere
that is served: ``scripts/remote_reader.py`` on an ordinary PC.

The server stays in charge of everything except the page load. It decides
what is due and when, exactly as before; a check for an adapter named in
``Settings.remote_reader_adapters`` is PARKED here instead of opening a
browser, the reader collects it over HTTPS (api/v1/reader.py), and the
offers it posts back go through the same ingest as any other fetch.

ONLY WHILE A READER IS ASKING. A PC is switched off at night and loses its
connection; the server must not wait on it. The reader's last poll is
remembered for ``remote_reader_alive_seconds``, and once that lapses the
worker reads the site itself again -- which is what it did before this
existed, and works on the days the site is not refusing it.

Redis only, no table: a parked check is worth nothing after half an hour and
there is nothing here an owner would ever look up.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import redis

from app.adapters.base import FetchResult, NormalizedOffer
from app.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import get_redis

log = get_logger("remote_reader")

#: The reader's last poll. Present = a reader is asking.
_SEEN = "reader:seen"
#: Parked checks, a hash keyed by hotel source and stay window, so a check
#: parked twice before the reader got to it is read once.
_JOBS = "reader:jobs"
#: A check the reader has taken and not yet answered.
_TAKEN = "reader:taken:"

#: A parked check older than this is about a moment that has gone: the same
#: half hour the broker gives a queued fetch (tasks_fetch._STALE_AFTER_SECONDS).
STALE_AFTER_SECONDS = 1800
#: How long the reader has to answer one it took. A Goibibo load is under a
#: minute; this allows for a slow one and a retry of the POST.
_TAKEN_TTL_SECONDS = 900


# -- who reads what ---------------------------------------------------
def adapters() -> frozenset[str]:
    """The adapter keys a reader may be handed. Empty when the key is unset."""
    settings = get_settings()
    if settings.remote_reader_key is None:
        return frozenset()
    return frozenset(k.strip() for k in settings.remote_reader_adapters.split(",") if k.strip())


def handles(adapter_key: str) -> bool:
    return adapter_key in adapters()


def alive() -> bool:
    """Whether a reader has asked for work recently enough to be given some."""
    try:
        return bool(get_redis().exists(_SEEN))
    except redis.RedisError as exc:
        # Unknown is "no": the worker then reads the page itself.
        log.warning("reader_seen_unreadable", error=str(exc)[:200])
        return False


def takes(adapter_key: str) -> bool:
    """Whether this check should be parked for the reader rather than run here."""
    return handles(adapter_key) and alive()


def mark_seen() -> None:
    get_redis().set(_SEEN, str(time.time()), ex=get_settings().remote_reader_alive_seconds)


# -- the queue --------------------------------------------------------
def _job_key(payload: dict[str, Any]) -> str:
    return (f"{payload['hotel_source_id']}:{payload['check_in']}:{payload['check_out']}:"
            f"{payload['adults']}:{payload['children']}:{payload['rooms']}")


def park(payload: dict[str, Any], check_run_id: str, triggered_by: str) -> str | None:
    """Leave one check for the reader. Returns the check run it replaced, if any.

    The same hotel and night parked again replaces the older one: the reader
    should load the page once, and the newer run is the one a page may be
    polling. The caller closes the replaced run.
    """
    r = get_redis()
    key = _job_key(payload)
    before = r.hget(_JOBS, key)
    job = {"id": uuid.uuid4().hex, "payload": payload, "check_run_id": check_run_id,
           "triggered_by": triggered_by, "parked_at": time.time()}
    r.hset(_JOBS, key, json.dumps(job))
    if before is None:
        return None
    replaced = json.loads(before).get("check_run_id")
    return replaced if replaced != check_run_id else None


def _is_stale(job: dict[str, Any], now: float, today: date) -> bool:
    if now - float(job["parked_at"]) > STALE_AFTER_SECONDS:
        return True
    return date.fromisoformat(job["payload"]["check_in"][:10]) < today


def sweep(today: date) -> list[dict[str, Any]]:
    """Remove the parked checks nobody collected in time, and return them."""
    r = get_redis()
    now = time.time()
    gone = []
    for key, raw in r.hgetall(_JOBS).items():
        job = json.loads(raw)
        if _is_stale(job, now, today) and r.hdel(_JOBS, key):
            gone.append(job)
    return gone


def take(today: date) -> dict[str, Any] | None:
    """The oldest parked check that is still worth reading, or nothing.

    Stale ones are left for :func:`sweep`, which also closes their check
    runs; this only refuses to hand them out.
    """
    r = get_redis()
    now = time.time()
    waiting = []
    for key, raw in r.hgetall(_JOBS).items():
        job = json.loads(raw)
        if not _is_stale(job, now, today):
            waiting.append((float(job["parked_at"]), key, job))
    for _parked_at, key, job in sorted(waiting, key=lambda w: w[0]):
        # HDEL says who got it, should two readers ever ask at once.
        if r.hdel(_JOBS, key):
            r.set(_TAKEN + job["id"], json.dumps(job), ex=_TAKEN_TTL_SECONDS)
            return job
    return None


def finish(job_id: str) -> dict[str, Any] | None:
    """The taken check an answer belongs to, once. ``None`` if it is unknown."""
    r = get_redis()
    raw = r.get(_TAKEN + job_id)
    if raw is None or not r.delete(_TAKEN + job_id):
        return None
    return json.loads(raw)


# -- offers over the wire ---------------------------------------------
_PRICES = ("price_inclusive", "price_exclusive", "taxes_fees")


def offer_to_json(offer: NormalizedOffer) -> dict[str, Any]:
    """One offer as JSON. Prices travel as strings: a float would round them."""
    out: dict[str, Any] = {
        "raw_room_name": offer.raw_room_name, "currency": offer.currency,
        "meal_plan": offer.meal_plan, "refundable": offer.refundable,
        "is_available": offer.is_available, "rooms_left": offer.rooms_left,
        "site_room_id": offer.site_room_id, "raw_payload": offer.raw_payload,
    }
    for name in _PRICES:
        value = getattr(offer, name)
        out[name] = None if value is None else str(value)
    return out


def offer_from_json(data: dict[str, Any]) -> NormalizedOffer:
    def price(name: str) -> Decimal | None:
        value = data.get(name)
        return None if value is None else Decimal(str(value))

    return NormalizedOffer(
        raw_room_name=str(data["raw_room_name"]),
        price_inclusive=price("price_inclusive"), price_exclusive=price("price_exclusive"),
        taxes_fees=price("taxes_fees"), currency=data.get("currency") or "INR",
        meal_plan=data.get("meal_plan"), refundable=data.get("refundable"),
        is_available=bool(data.get("is_available", True)), rooms_left=data.get("rooms_left"),
        site_room_id=data.get("site_room_id"), raw_payload=data.get("raw_payload"),
    )


def result_to_json(result: FetchResult) -> dict[str, Any]:
    return {"offers": [offer_to_json(o) for o in result.offers],
            "sold_out_detected": result.sold_out_detected, "duration_ms": result.duration_ms,
            "source_url": result.source_url, "notes": result.notes}


def result_from_json(data: dict[str, Any]) -> FetchResult:
    return FetchResult(
        offers=[offer_from_json(o) for o in data.get("offers") or []],
        sold_out_detected=bool(data.get("sold_out_detected")),
        duration_ms=data.get("duration_ms"), source_url=data.get("source_url"),
        notes=data.get("notes"),
    )
