"""robots.txt status handling, per RFC 9309 section 2.3.1.

These tests exist because getting this wrong is expensive in both directions
and silent in both directions. Too strict, and hotels that never objected are
refused — which is exactly what happened: a real property was marked
"DO NOT USE" because its CDN answers 403 for a file that does not exist. Too
lax, and a site that did object gets crawled anyway.

No network: ``_fetch`` is replaced with a canned status, which is the only
part that varies.
"""
from __future__ import annotations

import httpx
import pytest

from app.adapters.robots import RobotsChecker
from app.core.errors import RobotsDisallowedError

UA = "TestBot/1.0"
URL = "https://example.test/booking?checkin=2026-08-20"

DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ALLOW_WITH_DELAY = "User-agent: *\nAllow: /\nCrawl-delay: 5\n"
DISALLOW_BOOKING = "User-agent: *\nDisallow: /booking\n"


def _checker(monkeypatch, *, status: int, body: str = "") -> RobotsChecker:
    """A checker whose HTTP layer returns exactly one canned response."""
    checker = RobotsChecker(UA, cache=None, enabled=True)

    def fake_get(url, **_kwargs):
        return httpx.Response(status_code=status, text=body,
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return checker


class TestStatusHandling:
    def test_404_means_no_rules_and_is_allowed(self, monkeypatch):
        assert _checker(monkeypatch, status=404).check(URL).allowed is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 418])
    def test_every_4xx_permits_access(self, monkeypatch, status):
        """RFC 9309: a 4xx robots.txt is "unavailable", and access is permitted.

        403 is the one that matters. CloudFront and S3 answer 403 rather than
        404 for a missing object, so treating it as a refusal silently blocks
        every site hosted that way — including ones with no robots.txt at all.
        """
        assert _checker(monkeypatch, status=status).check(URL).allowed is True

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_disallows_everything(self, monkeypatch, status):
        """RFC 9309: "unreachable" means treat as a complete disallow.

        A server erroring on robots.txt cannot state its rules, and assuming
        permission while it is broken is the wrong default.
        """
        verdict = _checker(monkeypatch, status=status).check(URL)
        assert verdict.allowed is False

    def test_429_is_unknown_and_fails_open(self, monkeypatch):
        # Being throttled tells us nothing about the rules, and re-requesting
        # is the last thing a rate-limited server needs.
        assert _checker(monkeypatch, status=429).check(URL).allowed is True

    def test_network_error_fails_open(self, monkeypatch):
        checker = RobotsChecker(UA, cache=None, enabled=True)

        def boom(url, **_kwargs):
            raise httpx.ConnectError("dns")

        monkeypatch.setattr(httpx, "get", boom)
        # The page fetch will fail on its own and be classified properly; a DNS
        # blip must not permanently disable a source.
        assert checker.check(URL).allowed is True


class TestRuleEvaluation:
    def test_explicit_disallow_is_honoured(self, monkeypatch):
        verdict = _checker(monkeypatch, status=200, body=DISALLOW_ALL).check(URL)
        assert verdict.allowed is False
        assert "disallow" in verdict.reason.lower()

    def test_path_specific_disallow(self, monkeypatch):
        checker = _checker(monkeypatch, status=200, body=DISALLOW_BOOKING)
        assert checker.check("https://example.test/booking?x=1").allowed is False
        checker._local.clear()
        assert checker.check("https://example.test/rooms").allowed is True

    def test_crawl_delay_is_read(self, monkeypatch):
        verdict = _checker(monkeypatch, status=200, body=ALLOW_WITH_DELAY).check(URL)
        assert verdict.allowed is True
        assert verdict.crawl_delay == 5.0

    def test_assert_allowed_raises_a_non_retryable_error(self, monkeypatch):
        checker = _checker(monkeypatch, status=200, body=DISALLOW_ALL)
        with pytest.raises(RobotsDisallowedError) as excinfo:
            checker.assert_allowed(URL)
        # Never retried, and it disables the source: if a site says no, that is
        # not a technical problem to solve.
        assert excinfo.value.is_transient is False

    def test_assert_allowed_returns_the_verdict_when_permitted(self, monkeypatch):
        checker = _checker(monkeypatch, status=200, body=ALLOW_WITH_DELAY)
        assert checker.assert_allowed(URL).allowed is True

    def test_disabling_the_check_skips_it_entirely(self, monkeypatch):
        checker = RobotsChecker(UA, cache=None, enabled=False)
        verdict = checker.check(URL)
        assert verdict.allowed is True
        assert "disabled" in verdict.reason
