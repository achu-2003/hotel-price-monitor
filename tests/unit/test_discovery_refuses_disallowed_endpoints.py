"""What discovery may learn from, and what a silent robots.txt does not prove.

TWO THINGS THIS FILE HOLDS APART
================================
**A site said no.** gostops.com and api.gostops.com both publish
``Disallow: /api/*``. Pasting an Ooty hostel URL, discovery captured
``api.gostops.com/api/passes/v1/products/``, found nine "rooms" under
``data.0.hostels`` priced at a flat ₹300 -- the GoStops pass, not a dorm bed --
corroborated 8 of 8 against the page, because ₹300 really is printed on it, and
reported success. Attaching that would have configured the monitor to read a
disallowed path every half hour, for prices that were never room rates.

The robots problem was NOTICED: discovery wrote a note saying six endpoints
were off-limits and that "this hotel needs a DOM-based source". Then ``ok``
returned True anyway, because ``ok`` asks only whether the best candidate is
verified -- and notes are shown on failure. On success the warning was
discarded along with the rest.

**A server was down.** commonservice.ipms247.com answers 503 to /robots.txt.
RobotsChecker reads 5xx as a blanket disallow, which is right when the question
is "may we fetch this?" -- a server that cannot state its rules has granted
nothing. It is the wrong answer to "may we read what the page already fetched
in front of us?", and the first version of this fix proved it: Hotel Golden
Nest, a working hotel, had all ten of its availability endpoints declared
off-limits by an outage and discovery returned nothing at all.

So the rule is: drop a payload when the site refused it, keep it when we simply
could not ask. Both halves are pinned below, because either one alone is a bug.
"""
from __future__ import annotations

import pytest

from app.adapters import discovery
from app.adapters.robots import UNREADABLE_REASON, RobotsVerdict

ALLOWED = "https://letsbook.me/booking/metadata/currency.json"
REFUSED = "https://api.gostops.com/api/passes/v1/products/"
UNREADABLE = "https://commonservice.ipms247.com/YCSAPIServices/booking/getAvailability"

PAYLOADS = [(ALLOWED, {"a": 1}), (REFUSED, {"b": 2}), (UNREADABLE, {"c": 3})]


class _Checker:
    """Answers the three cases the real checker distinguishes."""

    def __init__(self, *_args, **_kwargs):
        pass

    def check(self, url: str) -> RobotsVerdict:
        if url == REFUSED:
            return RobotsVerdict(False, None, "disallowed by robots.txt")
        if url == UNREADABLE:
            return RobotsVerdict(False, None, UNREADABLE_REASON)
        return RobotsVerdict(True, None, "allowed by robots.txt")


@pytest.fixture
def dropped(monkeypatch):
    monkeypatch.setattr("app.adapters.robots.RobotsChecker", _Checker)
    kept, refused = discovery._drop_robots_disallowed(list(PAYLOADS))
    return [u for u, _ in kept], refused


class TestAnEndpointTheSiteRefused:
    def test_it_is_not_offered_as_evidence(self, dropped):
        kept, _refused = dropped
        assert REFUSED not in kept

    def test_it_is_reported_rather_than_dropped_silently(self, dropped):
        """The operator has to be able to tell "we found nothing" from "we
        found something we are not allowed to use"."""
        _kept, refused = dropped
        assert refused == [REFUSED]


class TestAnEndpointWeCouldNotAskAbout:
    """The 503 case. A file that could not answer has not said no."""

    def test_it_is_kept(self, dropped):
        kept, _refused = dropped
        assert UNREADABLE in kept

    def test_it_is_not_reported_as_a_refusal(self, dropped):
        _kept, refused = dropped
        assert UNREADABLE not in refused


class TestAnOrdinaryEndpoint:
    def test_it_is_kept(self, dropped):
        kept, _refused = dropped
        assert ALLOWED in kept


class TestTheFilterNeverCostsAFetch:
    def test_a_lookup_that_raises_keeps_the_payload(self, monkeypatch):
        """A robots lookup that fails proves nothing, and a hotel must not be
        unattachable because a DNS blip happened mid-probe."""

        class _Broken(_Checker):
            def check(self, url):
                raise RuntimeError("dns")

        monkeypatch.setattr("app.adapters.robots.RobotsChecker", _Broken)
        kept, refused = discovery._drop_robots_disallowed(list(PAYLOADS))

        assert len(kept) == 3 and refused == []

    def test_it_is_skipped_entirely_when_robots_checking_is_off(self, monkeypatch):
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "respect_robots_txt", False, raising=False)
        kept, refused = discovery._drop_robots_disallowed(list(PAYLOADS))

        assert len(kept) == 3 and refused == []

    def test_no_payloads_is_not_a_robots_lookup(self):
        assert discovery._drop_robots_disallowed([]) == ([], [])
