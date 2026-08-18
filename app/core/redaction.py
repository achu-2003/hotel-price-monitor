"""Secret scrubbing, enforced in code rather than by discipline.

Used in three places, so a secret cannot reach an operator's eyes by accident:
  * structlog processor        → application logs
  * Sentry ``before_send``     → error reports
  * ``scrub()`` before writing ``price_observations.raw_payload`` → the database

Matching is on the KEY name, recursively, at any depth.
"""
from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|secret|token|authorization|auth|cookie|session"
    r"|api[_-]?key|access[_-]?key|private[_-]?key|credential|kek|dek|otp|pin)",
    re.IGNORECASE,
)

# Values that look like secrets even when the key name is innocent.
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")

REDACTED = "***REDACTED***"
_MAX_DEPTH = 12


def _scrub_str(value: str) -> str:
    value = _BEARER_RE.sub(rf"\1{REDACTED}", value)
    return _LONG_TOKEN_RE.sub(REDACTED, value)


def scrub(value: Any, _depth: int = 0) -> Any:
    """Return ``value`` with every sensitive field replaced.

    Never raises: redaction failing must not take down the caller that was
    only trying to log something.
    """
    if _depth > _MAX_DEPTH:
        return "***TRUNCATED***"
    try:
        if isinstance(value, dict):
            return {
                k: (REDACTED if SENSITIVE_KEY_RE.search(str(k)) else scrub(v, _depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return type(value)(scrub(v, _depth + 1) for v in value)
        if isinstance(value, str):
            return _scrub_str(value)
        return value
    except Exception:  # noqa: BLE001 - redaction must never break logging
        return REDACTED


def structlog_processor(_logger: Any, _name: str, event_dict: dict) -> dict:
    return scrub(event_dict)  # type: ignore[return-value]


def sentry_before_send(event: dict, _hint: dict) -> dict:
    return scrub(event)  # type: ignore[return-value]
