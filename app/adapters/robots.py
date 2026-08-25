"""robots.txt enforcement.

The requirement is that access restrictions are respected, so this is enforced
in code rather than left to good intentions: every fetch passes through
:func:`assert_allowed`, and a disallowed path raises before any request is made
to the target page.

Deliberate design choices:

* **Status handling follows RFC 9309 section 2.3.1**, rather than intuition.
  4xx means "unavailable" and permits access; 5xx means "unreachable" and
  disallows everything. Reading a 403 as a refusal seems safer and is not:
  CloudFront and S3 answer 403 for a file that is simply absent, so sites with
  no robots.txt were being refused. Real refusals are caught on stronger
  evidence -- a bot wall or CAPTCHA raises ``BlockedError`` and stops the
  source outright.
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

#: The blanket disallow synthesised when robots.txt answers 5xx. It is a
#: SUBSTITUTE for rules, not rules: "we could not ask", rather than "they said
#: no", and the two have to stay tellable apart.
#:
#: Refusing to FETCH on either is right -- a server that cannot state its rules
#: has not granted anything. Refusing to READ evidence already in hand is right
#: only for a real prohibition. commonservice.ipms247.com answers 503 to
#: /robots.txt, so treating the two alike would let one outage declare a
#: hotel's own rates off-limits and strand it with no configuration at all.
_UNREADABLE = "# robots.txt unreadable\nUser-agent: *\nDisallow: /"
UNREADABLE_REASON = "robots.txt unreadable (server error), treated as disallow"


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

        # Status handling follows RFC 9309 section 2.3.1. Getting this wrong in
        # either direction is costly: too strict and we refuse sites that never
        # objected, too lax and we ignore one that did.
        if resp.status_code == 429:
            # Rate limited. We genuinely do not know the rules, and hammering
            # for them is the last thing a throttled server needs.
            log.warning("robots_rate_limited", url=robots_url)
            return None

        if 400 <= resp.status_code < 500:
            # "Unavailable" per the RFC: the crawler MAY access the site.
            #
            # This deliberately includes 401 and 403. An earlier version read a
            # 403 as a refusal, which sounds prudent and is wrong in practice:
            # CloudFront and S3 return 403 rather than 404 for a file that does
            # not exist, so a site with NO robots.txt was being treated as
            # having a blanket Disallow. That was observed on a real property,
            # and the same 403 was returned to an ordinary browser -- nothing
            # was refusing us specifically.
            #
            # Genuine refusals are still caught, just later and on better
            # evidence: a bot wall or CAPTCHA on the page itself raises
            # BlockedError, and that stops the source outright.
            log.info("robots_unavailable", url=robots_url, status=resp.status_code)
            return ""

        if resp.status_code >= 500:
            # "Unreachable" per the RFC: treat as a complete disallow. A server
            # erroring on robots.txt cannot tell us its rules, and assuming
            # permission while it is broken is the wrong default.
            log.warning("robots_server_error", url=robots_url, status=resp.status_code)
            return _UNREADABLE

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

        if allowed:
            reason = "allowed by robots.txt"
        elif text == _UNREADABLE:
            reason = UNREADABLE_REASON
        else:
            reason = "disallowed by robots.txt"

        return RobotsVerdict(
            allowed=allowed,
            crawl_delay=float(delay) if delay else None,
            reason=reason,
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
