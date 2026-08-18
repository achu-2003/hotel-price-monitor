"""structlog configuration: JSON in production, human-readable locally.

Every log line passes through the redaction processor (``app.core.redaction``)
before it is rendered, so credentials cannot leak into logs even if a caller
passes a whole request object.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.config import get_settings
from app.core.redaction import structlog_processor

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_configured = False


def _add_request_id(_logger: Any, _name: str, event_dict: dict) -> dict:
    rid = request_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_request_id,
        structlog_processor,  # ← redaction, always last before rendering
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # A stdlib logger factory, not PrintLoggerFactory: ``add_logger_name``
        # above reads ``logger.name``, which only a stdlib logger has. Pairing
        # it with a PrintLogger raises AttributeError on the FIRST log call in
        # the process — the kind of failure that only appears once something
        # else has already gone wrong.
        #
        # It also means Celery's and Uvicorn's own loggers land in the same
        # stream, through the same redaction processor, instead of bypassing it.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)  # type: ignore[return-value]
