"""Error taxonomy for price fetching.

Classification drives the retry policy (plan §11). Blind retries make outages
worse and get us blocked, so every failure is classified BEFORE we decide
whether to try again.

The two rules that matter most:
  * ``BlockedError`` / ``RobotsDisallowedError`` are NEVER retried. If a site
    does not want us there, we stop — we do not evade.
  * ``SchemaDriftError`` is never retried and never writes a price. A wrong
    price is far worse than a missing one.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    ROBOTS_DISALLOWED = "robots_disallowed"
    PARSE_SCHEMA_DRIFT = "parse_schema_drift"
    NO_AVAILABILITY = "no_availability"
    BROWSER_CRASH = "browser_crash"
    ADAPTER_CONFIG = "adapter_config"
    UNKNOWN = "unknown"


class FetchError(Exception):
    """Base class for anything that can go wrong fetching a price."""

    error_class: ErrorClass = ErrorClass.UNKNOWN
    is_transient: bool = False
    max_retries: int = 0

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}


# ── transient: worth retrying ────────────────────────────────────────
class NetworkError(FetchError):
    error_class = ErrorClass.NETWORK
    is_transient = True
    max_retries = 3


class TimeoutError_(FetchError):  # noqa: N801 - avoid shadowing builtins
    error_class = ErrorClass.TIMEOUT
    is_transient = True
    max_retries = 2


class HttpStatusError(FetchError):
    """5xx from the source. 4xx maps to a more specific class."""

    error_class = ErrorClass.HTTP_STATUS
    is_transient = True
    max_retries = 3

    def __init__(self, message: str, status_code: int, **kw) -> None:
        super().__init__(message, **kw)
        self.status_code = status_code
        self.context["status_code"] = status_code


class RateLimitedError(FetchError):
    """429. Honour Retry-After and halve this source's budget for an hour."""

    error_class = ErrorClass.RATE_LIMITED
    is_transient = True
    max_retries = 1

    def __init__(self, message: str, retry_after_seconds: int | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after_seconds = retry_after_seconds
        self.context["retry_after_seconds"] = retry_after_seconds


class BrowserCrashError(FetchError):
    error_class = ErrorClass.BROWSER_CRASH
    is_transient = True
    max_retries = 1


# ── permanent: do NOT retry ──────────────────────────────────────────
class BlockedError(FetchError):
    """A bot wall, CAPTCHA, or 403 appeared.

    We stop here by design. Working around it would mean defeating an access
    control, which this system does not do. The circuit opens and an operator
    decides what happens next.
    """

    error_class = ErrorClass.BLOCKED
    is_transient = False


class RobotsDisallowedError(FetchError):
    """robots.txt forbids this path. Hard stop, source disabled, no retry."""

    error_class = ErrorClass.ROBOTS_DISALLOWED
    is_transient = False


class AuthError(FetchError):
    error_class = ErrorClass.AUTH
    is_transient = False


class SchemaDriftError(FetchError):
    """The page loaded but did not contain what the adapter expected.

    Almost always a site redesign. Alert a human, save the artifacts, and
    write NO price.
    """

    error_class = ErrorClass.PARSE_SCHEMA_DRIFT
    is_transient = False


class AdapterConfigError(FetchError):
    error_class = ErrorClass.ADAPTER_CONFIG
    is_transient = False


class NoAvailabilityError(FetchError):
    """Not an error in the business sense: the room is legitimately sold out.

    Raised only when an adapter cannot distinguish "sold out" from "broken".
    Prefer returning an offer with ``is_available=False``.
    """

    error_class = ErrorClass.NO_AVAILABILITY
    is_transient = False


def classify(exc: BaseException) -> FetchError:
    """Map an arbitrary exception onto the taxonomy.

    Adapters should raise ``FetchError`` subclasses directly; this is the
    safety net for third-party exceptions that escape them.
    """
    if isinstance(exc, FetchError):
        return exc

    name = type(exc).__name__.lower()
    text = str(exc).lower()

    # Plenty of exceptions stringify to nothing at all -- NotImplementedError
    # is the common one, and an empty message is worse than no error row: it
    # says something broke and refuses to say what. The type name is always
    # available, so the message is never allowed to be blank.
    detail = str(exc).strip() or f"{type(exc).__name__} (no message)"

    if "timeout" in name or "timeout" in text:
        return TimeoutError_(detail)
    if any(k in name for k in ("connection", "dns", "socket", "ssl")):
        return NetworkError(detail)
    if "targetclosed" in name or ("browser" in text and "closed" in text):
        return BrowserCrashError(detail)
    return FetchError(detail, context={"exception_type": type(exc).__name__})
