"""robots.txt enforcement.

The requirement is that access restrictions are respected, so this is enforced
in code rather than left to good intentions: every fetch passes through
:func:`assert_allowed`, and a disallowed path raises before any request is made
to the target page.

Deliberate design choices:

* **Fail closed on a 4xx robots.txt that is not 404.** A 403 on robots.txt
  usually means a bot wall, which is a clear signal we are not welcome.
* **Fail open on a network error.** A transient DNS blip should not
  permanently disable a source; the page fetch itself will fail and be
  classified normally.
* **Honour Crawl-delay** when a site publishes one, on top of our own rate
  limit. If a site asks for slower, it gets slower.

Results are cached in Redis for 24 hours so we are not fetching robots.txt 90
times per cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.core.errors import RobotsDisallowedError
from app.core.logging import get_logger

log = get_logger("robots")

CACHE_TTL_SECONDS = 86_400
_FETCH_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class RobotsVerdict:
    allowed: bool
    crawl_delay: float | None
    reason: str


class RobotsChecker:
    """Fetches, caches and evaluates robots.txt.

    ``cache`` is any object with ``get``/``setex`` (Redis in production, a
    dict-backed fake in tests), so this class stays testable without a server.
    """

    def __init__(self, user_agent: str, cache=None, *, enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.cache = cache
        self.enabled = enabled
        self._local: dict[str, tuple[float, str | None]] = {}

    # ── fetching ─────────────────────────────────────────────────────
    def _robots_url(self, url: str) -> str:
        parts = urlparse(url)
        return f"{parts.scheme}://{parts.netloc}/robots.txt"

    def _fetch(self, robots_url: str) -> str | None:
        """Return robots.txt text, ``""`` when absent, or ``None`` on error.

        The three cases are distinct: absent means "no rules, allowed", while
        an error means "we do not know" and is handled fail-open by the caller.
        """
        try:
            resp = httpx.get(
                robots_url,
                timeout=_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        except httpx.HTTPError as exc:
            log.warning("robots_fetch_failed", url=robots_url, error=str(exc))
            return None

        if resp.status_code == 404:
            return ""  # no robots.txt at all: nothing is disallowed
        if resp.status_code == 403:
            # A robots.txt we are not allowed to read is itself a refusal.
            log.warning("robots_forbidden", url=robots_url)
            return "User-agent: *\nDisallow: /"
        if resp.status_code >= 400:
            log.warning("robots_bad_status", url=robots_url, status=resp.status_code)
            return None
        return resp.text

    def _cached_text(self, robots_url: str) -> str | None:
        now = time.time()
        if (entry := self._local.get(robots_url)) and entry[0] > now:
            return entry[1]

        if self.cache is not None:
            try:
                if (cached := self.cache.get(f"robots:{robots_url}")) is not None:
                    text = cached.decode() if isinstance(cached, bytes) else str(cached)
                    self._local[robots_url] = (now + 300, text)
                    return text
            except Exception as exc:  # noqa: BLE001 - cache must never block a fetch
                log.warning("robots_cache_read_failed", error=str(exc))

        text = self._fetch(robots_url)
        if text is not None:
            self._local[robots_url] = (now + 300, text)
            if self.cache is not None:
                try:
                    self.cache.setex(f"robots:{robots_url}", CACHE_TTL_SECONDS, text)
                except Exception as exc:  # noqa: BLE001
                    log.warning("robots_cache_write_failed", error=str(exc))
        return text

    # ── evaluation ───────────────────────────────────────────────────
    def check(self, url: str) -> RobotsVerdict:
        if not self.enabled:
            return RobotsVerdict(True, None, "robots checking disabled by configuration")

        robots_url = self._robots_url(url)
        text = self._cached_text(robots_url)

        if text is None:
            # Unknown. Fail open: the page request will fail on its own if the
            # site is genuinely unreachable, and that gets classified properly.
            return RobotsVerdict(True, None, "robots.txt unreachable, proceeding")
        if text == "":
            return RobotsVerdict(True, None, "no robots.txt")

        parser = RobotFileParser()
        parser.parse(text.splitlines())

        allowed = parser.can_fetch(self.user_agent, url)
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:  # noqa: BLE001 - malformed robots.txt
            delay = None

        return RobotsVerdict(
            allowed=allowed,
            crawl_delay=float(delay) if delay else None,
            reason="allowed by robots.txt" if allowed else "disallowed by robots.txt",
        )

    def assert_allowed(self, url: str) -> RobotsVerdict:
        """Raise ``RobotsDisallowedError`` if we may not fetch ``url``.

        This error is never retried and disables the source: if a site has said
        no, retrying is not a technical problem to solve.
        """
        verdict = self.check(url)
        if not verdict.allowed:
            log.warning("robots_disallowed", url=url)
            raise RobotsDisallowedError(
                f"robots.txt disallows fetching {url}",
                context={"url": url, "reason": verdict.reason},
            )
        return verdict
