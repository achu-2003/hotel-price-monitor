"""Redis-backed politeness: token buckets, distributed locks, penalty boxes.

Three concerns share this module because they share one Redis connection and
one failure philosophy.

**Fail open on Redis errors, with one exception.** If Redis is unreachable the
rate limiter allows the request: degrading into "slightly too fast" is better
than a total monitoring outage, and dispatch jitter still spreads the load.
The exception is the dispatch lock, which fails CLOSED — a lock we cannot read
must never be assumed free, or two workers drive two browsers at the same
hotel simultaneously.

Anything that must be atomic is a Lua script. A read-then-write token bucket
is a textbook race: with three browser workers it does not merely drift, it
reliably lets three requests through a one-token budget.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

import redis

from app.config import get_settings
from app.core.logging import get_logger

log = get_logger("ratelimit")


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """One connection pool per process.

    ``decode_responses`` is left off: the robots cache stores raw text and
    already accepts bytes or str, and binary payloads stay intact.
    """
    settings = get_settings()
    return redis.Redis.from_url(
        settings.cache_url,
        socket_timeout=5,
        socket_connect_timeout=3,
        health_check_interval=30,
        retry_on_timeout=True,
    )


# -- token bucket ----------------------------------------------------
# Refills continuously rather than in fixed windows: a fixed window lets a
# whole minute's budget fire in the last second of one window and the first
# second of the next, which is precisely the burst we are avoiding.
_BUCKET_LUA = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts     = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait = (1 - tokens) / rate
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(wait)}
"""


@dataclass(frozen=True, slots=True)
class BucketVerdict:
    allowed: bool
    retry_after_seconds: float


def _bucket_key(source_id: int) -> str:
    return f"ratelimit:source:{source_id}"


def _penalty_key(source_id: int) -> str:
    return f"ratelimit:penalty:{source_id}"


def effective_rate_per_min(source_id: int, configured: int) -> int:
    """The source's budget, halved while it is in the penalty box.

    A 429 means we asked for more than the site wants to give. Halving for an
    hour is the honest response; retrying at the same rate is how a temporary
    throttle becomes a permanent block.
    """
    try:
        if get_redis().exists(_penalty_key(source_id)):
            return max(1, configured // 2)
    except redis.RedisError as exc:
        log.warning("penalty_check_failed", source_id=source_id, error=str(exc))
    return max(1, configured)


def penalise_source(source_id: int, *, seconds: int = 3600) -> None:
    """Halve this source's budget for an hour. Called on HTTP 429."""
    try:
        get_redis().setex(_penalty_key(source_id), seconds, b"1")
        log.warning("source_rate_budget_halved", source_id=source_id, seconds=seconds)
    except redis.RedisError as exc:
        log.warning("penalty_set_failed", source_id=source_id, error=str(exc))


def take_token(source_id: int, rate_per_min: int) -> BucketVerdict:
    """Spend one request against this source's budget.

    Returns how long to wait when refused, so the caller can defer the task
    rather than block a browser worker doing nothing.
    """
    capacity = max(1, rate_per_min)
    rate = capacity / 60.0
    try:
        raw = get_redis().eval(
            _BUCKET_LUA, 1, _bucket_key(source_id),
            capacity, rate, time.time(), 3600,
        )
    except redis.RedisError as exc:
        # Fail open: a Redis outage must not stop all price monitoring.
        log.warning("rate_limit_unavailable", source_id=source_id, error=str(exc))
        return BucketVerdict(True, 0.0)

    allowed = bool(int(raw[0]))
    try:
        wait = float(raw[1])
    except (TypeError, ValueError):
        wait = 60.0 / rate if rate else 60.0
    return BucketVerdict(allowed, 0.0 if allowed else max(1.0, wait))


# -- distributed lock ------------------------------------------------
# Compare-and-delete on release: without it, a task that overran its TTL would
# delete a lock a DIFFERENT worker had since acquired.
_UNLOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class LockNotAcquired(RuntimeError):
    """Someone else holds this lock. Not an error — skip this run."""


@contextmanager
def dispatch_lock(name: str, ttl_seconds: int) -> Generator[str, None, None]:
    """Hold ``name`` for at most ``ttl_seconds``.

    Raises :class:`LockNotAcquired` when it is already held, including when
    Redis is unreachable. This is the one place we fail CLOSED: a missed check
    costs one stale data point, while a phantom-free lock puts two browsers on
    the same hotel at once.
    """
    token = uuid.uuid4().hex
    client = get_redis()
    try:
        acquired = client.set(name, token, nx=True, ex=ttl_seconds)
    except redis.RedisError as exc:
        log.warning("lock_unavailable", lock=name, error=str(exc))
        raise LockNotAcquired(f"Redis unavailable while acquiring {name}") from exc

    if not acquired:
        raise LockNotAcquired(name)

    try:
        yield token
    finally:
        try:
            client.eval(_UNLOCK_LUA, 1, name, token)
        except redis.RedisError as exc:  # noqa: BLE001 - the TTL cleans up
            log.warning("lock_release_failed", lock=name, error=str(exc))


# -- per-recipient notification throttle -----------------------------
def recipient_quota_remaining(recipient_id: int, max_per_hour: int) -> int:
    """How many more messages this person may receive this hour.

    Separate from the token bucket because the semantics differ: this is a
    hard cap on how much we are willing to interrupt someone, not a politeness
    budget that refills smoothly.
    """
    key = f"notify:quota:{recipient_id}:{int(time.time() // 3600)}"
    try:
        used = int(get_redis().get(key) or 0)
        return max(0, max_per_hour - used)
    except (redis.RedisError, ValueError) as exc:
        log.warning("quota_read_failed", recipient_id=recipient_id, error=str(exc))
        return max_per_hour  # fail open: a Redis blip must not silence alerts


def consume_recipient_quota(recipient_id: int) -> None:
    key = f"notify:quota:{recipient_id}:{int(time.time() // 3600)}"
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, 7200)
        pipe.execute()
    except redis.RedisError as exc:
        log.warning("quota_write_failed", recipient_id=recipient_id, error=str(exc))
