"""robots.txt enforcement.

The requirement is that access restrictions are respected, so this is enforced
in code rather than left to good intentions: every fetch passes through
:func:`assert_allowed`, and a disallowed path raises before any request is made
to the target page.

Deliberate design choices:

* **Rule matching follows RFC 9309 section 2.2.2**: the LONGEST matching
  path wins, and ``Allow`` wins a tie. This is implemented here rather than
  delegated to :class:`urllib.robotparser.RobotFileParser`, whose
  ``can_fetch`` returns the FIRST matching rule in file order. The difference
  is not academic. Two of this project's hotels sit behind Hotelzify, whose
  robots.txt opens with a blanket ``Allow: /`` and closes the booking paths
  underneath it::

      User-agent: *
      Allow: /
      Disallow: /rooms/

  ``/rooms/5171/...`` is what the monitor fetches. Under the RFC the
  seven-character ``Disallow: /rooms/`` beats the one-character ``Allow: /``
  and the answer is no. Under first-match the ``Allow: /`` on the line above
  wins and the answer is yes -- so the check ran, passed, and the fetch went
  ahead against a path the site had closed, for as long as those hotels were
  monitored. A robots check that cannot say no is not a check.

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

import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.core.errors import RobotsDisallowedError
from app.core.logging import get_logger

log = get_logger("robots")

CACHE_TTL_SECONDS = 86_400
#: How long a FAILED robots.txt fetch is remembered, process-locally.
#: Short, because it describes this moment rather than the site's rules.
NEGATIVE_CACHE_SECONDS = 60
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


#: A ``user-agent`` line, or a rule line. Comments run to end of line and a
#: malformed line is skipped rather than fatal: robots.txt is written by hand
#: far more often than it is generated, and one stray line must not decide that
#: a site has no rules at all.
_RULE_KEYS = ("allow", "disallow")


def _groups(text: str) -> dict[str, list[tuple[str, str]]]:
    """Parse robots.txt into ``{user-agent: [(key, value), ...]}``.

    Consecutive ``User-agent`` lines share the group that follows them, which
    is how a single block addresses several crawlers (RFC 9309 s2.2.1). A
    ``User-agent`` line appearing AFTER a rule starts a new group.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    starting_new_group = True

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()

        if key == "user-agent":
            if not starting_new_group:
                agents = []
                starting_new_group = True
            agents.append(value.lower())
            groups.setdefault(value.lower(), [])
            continue

        starting_new_group = False
        for agent in agents:
            groups.setdefault(agent, []).append((key, value))

    return groups


def _rules_for(text: str, user_agent: str) -> list[tuple[str, str]]:
    """The one group that applies to us (RFC 9309 s2.2.1).

    Groups are NOT merged: the most specific matching user-agent wins outright,
    and ``*`` is the fallback used only when no named group matches. Merging
    them would let a permissive global group loosen a strict named one, which
    is the failure the Hotelzify robots.txt calls out in its own comments.
    """
    groups = _groups(text)
    product_token = user_agent.split("/", 1)[0].strip().lower()

    best: str | None = None
    for agent in groups:
        if agent == "*":
            continue
        # Substring either way: a robots.txt naming "hotelpricemonitor"
        # addresses "HotelPriceMonitor/1.0 (+https://...)", and a robots.txt
        # naming the full string addresses a bare token.
        if agent and (agent in product_token or product_token in agent):
            if best is None or len(agent) > len(best):
                best = agent

    return groups.get(best if best is not None else "*", [])


def _path_of(url: str) -> str:
    """The part a rule is matched against: path plus query (RFC 9309 s2.2.2)."""
    parts = urlparse(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _matches(pattern: str, path: str) -> bool:
    """RFC 9309 s2.2.3 path matching: ``*`` is any run, ``$`` anchors the end.

    Everything else is a literal prefix, so ``/rooms/`` matches
    ``/rooms/5171/2026-08-26`` and does not match ``/room-view/``.
    """
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]

    expression = ".*".join(re.escape(chunk) for chunk in pattern.split("*"))
    if anchored:
        expression += "$"
    return re.match(expression, path) is not None


def rule_allows(text: str, user_agent: str, url: str) -> bool:
    """Does ``text`` permit ``user_agent`` to fetch ``url``?

    RFC 9309 s2.2.2: of every rule whose path matches, the one with the
    LONGEST path wins; ``Allow`` wins a tie. With no matching rule the answer
    is yes -- robots.txt is a list of exceptions to permission, not a grant.

    An empty ``Disallow:`` is the documented way to say "nothing is
    forbidden", so it is skipped rather than treated as a zero-length rule
    that would match, and lose, every path.
    """
    path = _path_of(url)

    winner: tuple[int, str] | None = None
    for key, value in _rules_for(text, user_agent):
        if key not in _RULE_KEYS or not value:
            continue
        if not _matches(value, path):
            continue
        # On equal length Allow wins, and it is reached by comparing STRICTLY
        # greater so that an Allow already recorded is not displaced by a
        # Disallow of the same length.
        if winner is None or len(value) > winner[0]:
            winner = (len(value), key)
        elif len(value) == winner[0] and key == "allow":
            winner = (len(value), key)

    return True if winner is None else winner[1] == "allow"


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
        else:
            # A host that just failed to serve robots.txt will fail again a
            # second later, and every attempt pays the full _FETCH_TIMEOUT.
            # During one 27-minute outage at a single property that cost 66
            # connections and about eleven minutes of worker time, spent
            # hammering an origin that was already struggling.
            #
            # Remembered PROCESS-LOCALLY only, and briefly. The shared cache
            # holds what a site's rules are; it must never carry the fact that
            # the site was unreachable once, to every other worker, for a day.
            # check() fails open on None, so nothing about permission changes
            # here -- only how often we ask.
            self._local[robots_url] = (now + NEGATIVE_CACHE_SECONDS, None)
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

        # Rule matching is ours (see the module docstring); the stdlib parser
        # is kept only for Crawl-delay, which is an extension it already reads
        # and which has no matching semantics to get wrong.
        allowed = rule_allows(text, self.user_agent, url)
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
