"""The state of a login test in progress, shared between the API and the worker.

A test that may need a one-time code cannot be one request: the browser
sits open on the worker waiting for the owner to read their email, and the
page has to be told "type the code now" and then hand it over. Redis is the
meeting point -- the API and the browser worker are different processes on
possibly different machines, and Redis is what they already share.

One test per owner at a time, keyed by owner: a second click while one is
waiting joins it rather than starting a second browser.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.core.ratelimit import get_redis

#: How long the browser waits for the owner to type the code. Bounded by the
#: worker's soft time limit (300s) less the login itself; three minutes is
#: enough to open an email and copy six digits, and short enough that an
#: abandoned test does not hold a browser slot for the rest of the hour.
CODE_WAIT_SECONDS = 180

#: A state older than this is a test that never finished -- a worker that
#: died mid-login -- and is treated as no test at all.
_STALE_SECONDS = 420


def _key(owner_user_id: int, kind: str = "test") -> str:
    return f"rate_app:{kind}:{owner_user_id}"


def _code_key(owner_user_id: int) -> str:
    return f"rate_app:test:{owner_user_id}:code"


def read(owner_user_id: int, kind: str = "test") -> dict[str, Any] | None:
    raw = get_redis().get(_key(owner_user_id, kind))
    if not raw:
        return None
    state = json.loads(raw)
    updated = datetime.fromisoformat(state["updated_at"])
    if state["status"] != "done" and (datetime.now(UTC) - updated).total_seconds() > _STALE_SECONDS:
        return None
    return state


def write(owner_user_id: int, status: str, kind: str = "test", **fields: Any) -> dict[str, Any]:
    """``kind`` separates the login test's state from a repricing run's: the
    two are different jobs with different pages watching them."""
    state = {"status": status, "updated_at": datetime.now(UTC).isoformat(), **fields}
    get_redis().set(_key(owner_user_id, kind), json.dumps(state, default=str), ex=_STALE_SECONDS + 60)
    return state


def clear(owner_user_id: int) -> None:
    r = get_redis()
    r.delete(_key(owner_user_id))
    r.delete(_code_key(owner_user_id))


def offer_code(owner_user_id: int, code: str) -> None:
    """The owner typed the code; leave it where the waiting worker looks."""
    get_redis().set(_code_key(owner_user_id), code, ex=CODE_WAIT_SECONDS)


def take_code(owner_user_id: int) -> str | None:
    """The code, once, or ``None``. Deleted on read so it cannot be replayed.

    GET then DELETE rather than GETDEL: the Redis on the Windows boxes is a
    3.x build that has never heard of GETDEL. The gap between the two is a
    millisecond on one key only this worker reads, and a replay inside it
    would type the same code into the same screen.
    """
    r = get_redis()
    key = _code_key(owner_user_id)
    raw = r.get(key)
    if raw:
        r.delete(key)
    return raw.decode() if isinstance(raw, bytes) else raw
