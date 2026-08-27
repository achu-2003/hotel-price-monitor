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


#: The shape that made this necessary. Hotelzify serves two of the project's
#: hotels and opens its robots.txt with a blanket Allow, then closes the
#: booking paths underneath it. Reproduced from the live file.
HOTELZIFY = (
    "User-agent: *\n"
    "Allow: /\n"
    "Allow: /static/\n"
    "Disallow: /rooms/\n"
    "Disallow: /room-view/\n"
    "Disallow: /checkout\n"
    "Disallow: /api/\n"
)


class TestLongestMatchWins:
    """RFC 9309 s2.2.2. The stdlib parser returns the FIRST matching rule in
    file order, which reads this file as wide open and let the monitor fetch
    /rooms/ for as long as those two hotels were watched."""

    @pytest.mark.parametrize(
        "path,allowed",
        [
            ("/rooms/5171/2026-08-26/2026-08-27/2/0", False),  # the real URL
            ("/room-view/9", False),
            ("/checkout", False),
            ("/api/v1/availability", False),
            ("/", True),
            ("/static/app.css", True),
            ("/offers", True),
        ],
    )
    def test_a_specific_disallow_beats_a_blanket_allow_above_it(
        self, monkeypatch, path, allowed
    ):
        checker = _checker(monkeypatch, status=200, body=HOTELZIFY)
        assert checker.check(f"https://example.test{path}").allowed is allowed

    def test_order_does_not_change_the_answer(self, monkeypatch):
        """The same rules written the other way round must decide the same."""
        reversed_file = (
            "User-agent: *\n"
            "Disallow: /rooms/\n"
            "Allow: /\n"
        )
        url = "https://example.test/rooms/5171"
        assert _checker(monkeypatch, status=200, body=HOTELZIFY).check(url).allowed is False
        assert _checker(monkeypatch, status=200, body=reversed_file).check(url).allowed is False

    def test_allow_wins_a_tie_of_equal_length(self, monkeypatch):
        body = "User-agent: *\nDisallow: /book\nAllow: /book\n"
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/book").allowed is True

    def test_a_longer_allow_reopens_a_disallowed_subtree(self, monkeypatch):
        body = "User-agent: *\nDisallow: /rooms/\nAllow: /rooms/public/\n"
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/rooms/5171").allowed is False
        assert checker.check("https://example.test/rooms/public/1").allowed is True

    def test_empty_disallow_forbids_nothing(self, monkeypatch):
        """'Disallow:' with no value is the documented way to say 'everything
        is permitted'. Treated as a zero-length rule it would match every path
        and lose every comparison, which is right by accident -- but it would
        also be the winner on a file that has no other rule, which is not."""
        checker = _checker(monkeypatch, status=200, body="User-agent: *\nDisallow:\n")
        assert checker.check("https://example.test/anything").allowed is True

    def test_wildcard_and_end_anchor(self, monkeypatch):
        body = "User-agent: *\nDisallow: /*.pdf$\n"
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/docs/rates.pdf").allowed is False
        assert checker.check("https://example.test/docs/rates.pdf.html").allowed is True

    def test_the_query_string_is_matched_too(self, monkeypatch):
        body = "User-agent: *\nDisallow: /search?sort=price\n"
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/search?sort=price").allowed is False
        assert checker.check("https://example.test/search?sort=name").allowed is True


class TestGroupSelection:
    """RFC 9309 s2.2.1: the most specific user-agent group applies, and groups
    are never merged."""

    def test_a_named_group_replaces_the_wildcard_group(self, monkeypatch):
        body = (
            "User-agent: *\n"
            "Disallow: /\n"
            "\n"
            "User-agent: TestBot\n"
            "Allow: /\n"
        )
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/rooms/1").allowed is True

    def test_a_named_group_is_not_loosened_by_the_wildcard_group(self, monkeypatch):
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "\n"
            "User-agent: TestBot\n"
            "Disallow: /\n"
        )
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/anything").allowed is False

    def test_consecutive_user_agent_lines_share_one_group(self, monkeypatch):
        body = (
            "User-agent: SomeoneElse\n"
            "User-agent: TestBot\n"
            "Disallow: /rooms/\n"
        )
        checker = _checker(monkeypatch, status=200, body=body)
        assert checker.check("https://example.test/rooms/1").allowed is False
        assert checker.check("https://example.test/other").allowed is True
