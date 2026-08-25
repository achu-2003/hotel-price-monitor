"""A page that never stops fetching things is still a page we can read.

THE BUG THIS PINS
=================
``inspect_url`` navigated with ``wait_until="networkidle"``, which means "no
network request for 500ms". A large OTA never offers that: analytics beacons,
session heartbeats, lazy imagery and third-party frames keep traffic moving for
as long as the tab is open. Because the condition was attached to the
NAVIGATION, a page that had rendered its rates perfectly well still ended the
whole probe with a raw Playwright ``TimeoutError``, and "Detect and attach"
reported

    Could not inspect that page: TimeoutError.

The tell was that it depended on the machine, not on the site. The same
Cleartrip URL attached on one laptop and failed on another running identical
code, because whether a busy page happens to draw breath for half a second is a
race between the connection, the CPU and whatever the trackers are doing.

So the wait moved off the navigation: load the document, then ASK for quiet
with its own budget and carry on without it. Every JSON response the page
fetched in the meantime was already captured by the listener, which is the only
thing the wait was ever for.

No browser is launched here. What is being tested is the sequence of waits and
what survives one of them running out.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.adapters import discovery
from app.core.errors import TimeoutError_

PAGE_TEXT = "Deluxe Room ₹4,200 per night\nSuite ₹7,600 per night"


class FakePage:
    """Enough of a Playwright page for the navigation block to run.

    ``idle`` decides whether the page ever goes quiet, which is the whole
    subject of this file.
    """

    def __init__(self, *, idle: bool, loads: bool = True,
                 body_reads_after_ms: int = 0) -> None:
        self.idle = idle
        self.loads = loads
        #: The body is legible only once this much patience is offered.
        self.body_reads_after_ms = body_reads_after_ms
        self.goto_args: dict = {}
        self.load_states: list[str] = []
        self.slept_ms: list[int] = []
        self.body_budgets: list[int] = []

    def on(self, _event, _handler) -> None:
        pass

    def goto(self, url, *, wait_until, timeout):
        self.goto_args = {"url": url, "wait_until": wait_until, "timeout": timeout}
        if not self.loads:
            raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded.")

    def wait_for_load_state(self, state, *, timeout):
        self.load_states.append(state)
        if not self.idle:
            raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded.")

    def wait_for_timeout(self, ms) -> None:
        self.slept_ms.append(ms)

    def inner_text(self, _selector, timeout=None) -> str:  # noqa: ARG002
        self.body_budgets.append(timeout)
        if timeout is not None and timeout < self.body_reads_after_ms:
            raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded.")
        return PAGE_TEXT

    def query_selector(self, _selector):
        return None

    def close(self) -> None:
        pass


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def new_page(self) -> FakePage:
        return self._page


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.closed = False

    def new_context(self, **_kwargs) -> FakeContext:
        return FakeContext(self._page)

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, page: FakePage) -> None:
        self.chromium = self
        self._page = page
        self.browser = FakeBrowser(page)

    def launch(self, **_kwargs) -> FakeBrowser:
        return self.browser


@pytest.fixture
def page_that(monkeypatch):
    """Run ``inspect_url`` against a page whose behaviour the test chooses."""

    def _run(*, idle: bool, loads: bool = True,
             body_reads_after_ms: int = 0) -> tuple[FakePage, object]:
        page = FakePage(idle=idle, loads=loads,
                        body_reads_after_ms=body_reads_after_ms)

        @contextmanager
        def _fake_playwright():
            yield FakePlaywright(page)

        monkeypatch.setattr(discovery, "_playwright_for_this_thread", _fake_playwright)
        # The bot-wall check and the DOM fallback both want a real page.
        monkeypatch.setattr(
            "app.adapters.playwright_base.detect_bot_wall", lambda _page: None
        )
        monkeypatch.setattr(
            "app.adapters.dom_discovery.find_room_cards", lambda _page: []
        )
        result = discovery.inspect_url("https://www.cleartrip.com/hotels/details/4670812")
        return page, result

    return _run


class TestAPageThatNeverGoesQuiet:
    def test_it_is_still_inspected(self, page_that):
        """The failure the operator saw: a loaded page, reported as a timeout.

        Nothing about this page is wrong. It renders its rates and it keeps a
        heartbeat open, like every large OTA does.
        """
        _page, result = page_that(idle=False)

        assert result is not None  # it returned a verdict rather than raising

    def test_the_navigation_does_not_wait_for_the_network(self, page_that):
        page, _result = page_that(idle=False)

        assert page.goto_args["wait_until"] == "domcontentloaded"

    def test_quiet_is_asked_for_separately(self, page_that):
        """Still asked for -- a page that does settle is worth waiting for."""
        page, _result = page_that(idle=False)

        assert page.load_states == ["networkidle"]

    def test_a_page_that_never_settled_gets_longer_to_paint(self, page_that):
        """Nothing has told us its rates arrived, so the grace period is all
        it has."""
        busy, _ = page_that(idle=False)
        quiet, _ = page_that(idle=True)

        assert busy.slept_ms[0] > quiet.slept_ms[0]

    def test_the_result_says_the_page_never_settled(self, page_that):
        """An empty finding has to explain itself, or the next person re-runs
        it and gets the same nothing."""
        _page, result = page_that(idle=False)

        assert not result.ok
        assert any("never stopped fetching" in note for note in result.notes)


class TestAPageThatDoesNotLoadAtAll:
    def test_it_is_reported_in_words(self, page_that):
        """Still a failure -- but one that names which wait ran out, because
        "TimeoutError" told an operator nothing about what to do next."""
        with pytest.raises(TimeoutError_) as caught:
            page_that(idle=False, loads=False)

        assert "did not finish loading" in str(caught.value)


class TestAPageTooHeavyToReadAtOnce:
    """The OTHER wait that ended the probe with a bare "TimeoutError".

    Reading the body of a heavy page is not always a five-second job, and
    the failure was indistinguishable -- to whoever read the message -- from
    the navigation timing out three lines above it.
    """

    def test_the_body_is_given_a_second_chance(self, page_that):
        page, result = page_that(idle=True, body_reads_after_ms=12_000)

        # The last two, not the first two: the widget check reads the
        # body first, on its own five-second budget.
        assert page.body_budgets[-2:] == [5_000, 15_000]
        assert result is not None

    def test_a_body_that_never_reads_is_reported_in_words(self, page_that):
        """And NOT by falling back to the page source. Corroboration asks
        whether a price is on screen; matching raw HTML would confirm prices
        out of scripts and hidden markup, which is a config that looks
        verified and is not."""
        with pytest.raises(TimeoutError_) as caught:
            page_that(idle=True, body_reads_after_ms=60_000)

        assert "could not read the text of the page" in str(caught.value)
