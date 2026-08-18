"""Shared response shapes.

Two decisions worth stating once, here, rather than repeating in every module:

**Cursor pagination, not offsets.** ``price_observations`` grows forever, and
``OFFSET 50000`` makes Postgres walk 50,000 rows to discard them. Worse, on a
table receiving inserts, page 2 of an offset query silently skips rows that
page 1 pushed down. A keyset cursor has neither problem.

**RFC 7807 problem details for errors.** One error shape everywhere means the
dashboard has one error renderer, and a machine consuming the API can tell a
validation failure from a permission failure without parsing prose.
"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for anything read out of a SQLAlchemy row."""

    model_config = ConfigDict(from_attributes=True)


class ProblemDetail(BaseModel):
    """RFC 7807. Returned for every 4xx and 5xx."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    #: Present on 422 so a form can highlight the offending field.
    errors: list[dict] | None = None


class Page(BaseModel, Generic[T]):
    """One page of results plus the cursor for the next one.

    ``next_cursor`` is ``None`` on the last page. Clients must treat the
    cursor as opaque — it is an encoded key, not an index, and its format is
    free to change.
    """

    items: list[T]
    next_cursor: str | None = None
    total: int | None = Field(
        default=None,
        description=(
            "Only populated where a count is cheap. Absent on the history and "
            "observation endpoints, where COUNT(*) would defeat the point of "
            "cursor pagination."
        ),
    )


class PaginationParams(BaseModel):
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class HealthStatus(BaseModel):
    status: str
    version: str = "1.0"
    checked_at: datetime


class ReadinessStatus(BaseModel):
    """Liveness answers "is the process up"; readiness answers "can it work".

    Kept distinct because an orchestrator that restarts a container for a
    database blip turns a thirty-second outage into a restart loop.
    """

    status: str
    database: bool
    redis: bool
    detail: str | None = None


class AcceptedRun(BaseModel):
    """202 response for anything that hands work to a worker.

    A browser fetch takes 20-40 seconds. An HTTP request must never wait on
    one, so the manual-trigger endpoint returns the run id and the dashboard
    polls ``/check-runs/{id}``.
    """

    check_run_id: str
    status: str = "queued"
    poll_url: str
