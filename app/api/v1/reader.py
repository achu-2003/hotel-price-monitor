"""Where the reader PC collects checks and posts what it read.

See services/remote_reader.py for why a page is read away from the server.
Two calls, both made BY the reader, so the PC needs no open port, no fixed
address and nothing forwarded on its router:

    POST /api/v1/reader/next     -> one parked check, or none
    POST /api/v1/reader/result   <- the offers it read, or the error it hit

Not a user endpoint: no login, one shared key (Settings.remote_reader_key) in
``X-Reader-Key``. Without the key configured the routes answer 404, so a
deployment that never set a reader up does not advertise one.

Plain ``def`` handlers: Redis and the ingest are synchronous, and FastAPI
runs these on its thread pool rather than on the event loop.
"""
from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, status

from app.config import get_settings
from app.core.logging import get_logger
from app.services import remote_reader
from app.services.dates import local_today

router = APIRouter(prefix="/reader", tags=["reader"])
log = get_logger("api.reader")

#: How long the reader should wait before asking again when there is nothing.
IDLE_POLL_SECONDS = 20


def _authorise(key: str | None) -> None:
    expected = get_settings().remote_reader_key
    if expected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if not key or not hmac.compare_digest(key.encode(), expected.get_secret_value().encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_key")


@router.post("/next")
def next_check(x_reader_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Say "I am here", and take the oldest parked check if there is one."""
    _authorise(x_reader_key)
    remote_reader.mark_seen()
    job = remote_reader.take(local_today(get_settings().timezone))
    if job is None:
        return {"job": None, "wait_seconds": IDLE_POLL_SECONDS,
                "adapters": sorted(remote_reader.adapters())}
    log.info("reader_took_check", hotel=job["payload"]["hotel_name"],
             check_in=job["payload"]["check_in"])
    return {"job": {"id": job["id"], "payload": job["payload"],
                    "check_run_id": job["check_run_id"]},
            "wait_seconds": 0, "adapters": sorted(remote_reader.adapters())}


@router.post("/result")
def post_result(
    body: dict[str, Any] = Body(...),
    x_reader_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """What the reader found for a check it took: offers, or an error."""
    _authorise(x_reader_key)
    remote_reader.mark_seen()
    job = remote_reader.finish(str(body.get("job_id") or ""))
    if job is None:
        # Answered already, or taken so long ago it was given up on.
        raise HTTPException(status.HTTP_410_GONE, "unknown_or_expired_job")

    # Imported here: the task module pulls in Celery and every adapter, which
    # the API has no other reason to load at start-up.
    from app.workers.tasks_fetch import finish_remote_read

    return finish_remote_read(job, body)
