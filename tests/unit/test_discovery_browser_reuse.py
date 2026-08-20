"""Which Playwright instance discovery is allowed to use.

THE BUG THIS PINS
=================
``BrowserPool`` starts a sync Playwright per thread and, by design, never stops
it — one browser per worker for the process lifetime is the whole point.
``inspect_url`` used to start its own unconditionally, which is correct from the
API process and fatal from a Celery browser worker: from that worker's first
fetch onwards the thread permanently has a live sync Playwright, and a second
one raises

    It looks like you are using Playwright Sync API inside the asyncio loop.

This did not show up while discovery only ran from the dashboard. It appeared
the moment automatic repair started calling it from the worker that had just
done the fetch — which is every repair after the first task on that worker, in
other words all of them.

The two callers need opposite things, so the choice cannot be a constant. These
tests fix the rule: borrow if the pool has one in THIS thread, otherwise start a
private one, and never stop something the pool owns.

No browser is launched here. What is being tested is the decision.
"""
from __future__ import annotations

import threading

import pytest

from app.adapters import discovery
from app.adapters.playwright_base import browser_pool


class _FakePlaywright:
    """Stands in for a live instance the pool owns."""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def __exit__(self, *_exc) -> None:  # pragma: no cover - must never be called
        raise AssertionError("the borrowed instance was closed as a context manager")


@pytest.fixture
def clean_pool():
    """Leave this thread's pool slot exactly as it was found."""
    had = hasattr(browser_pool._local, "playwright")
    previous = getattr(browser_pool._local, "playwright", None)
    try:
        yield
    finally:
        if had:
            browser_pool._local.playwright = previous
        elif hasattr(browser_pool._local, "playwright"):
            del browser_pool._local.playwright


class TestWhenThePoolAlreadyHasOne:
    def test_the_live_instance_is_borrowed(self, clean_pool):
        live = _FakePlaywright()
        browser_pool._local.playwright = live

        with discovery._playwright_for_this_thread() as playwright:
            assert playwright is live

    def test_the_borrowed_instance_is_not_stopped(self, clean_pool):
        """The pool owns it and other fetches on this thread are still using
        it. Stopping it here would break every subsequent fetch in the worker,
        and the error would surface at the fetch rather than here."""
        live = _FakePlaywright()
        browser_pool._local.playwright = live

        with discovery._playwright_for_this_thread():
            pass

        assert not live.stopped
        assert browser_pool.current_playwright is live

    def test_no_second_instance_is_started(self, clean_pool, monkeypatch):
        """The actual failure: starting a second sync Playwright in a thread
        that already has one raises inside Playwright itself."""
        import playwright.sync_api

        def _refuse():  # pragma: no cover - the assertion is that this is unused
            raise AssertionError(
                "started a second Playwright while the pool's was live -- this "
                "is the asyncio-loop crash this fix exists to prevent"
            )

        monkeypatch.setattr(playwright.sync_api, "sync_playwright", _refuse)
        browser_pool._local.playwright = _FakePlaywright()

        with discovery._playwright_for_this_thread():
            pass


class TestWhenThePoolHasNone:
    def test_a_private_instance_is_started_and_stopped(self, clean_pool, monkeypatch):
        """The API process has no pool instance, and must not be given one:
        the sync API is bound to its creating thread, so a web request handed
        to a threadpool cannot borrow a singleton."""
        started = _FakePlaywright()
        events = []

        class _Manager:
            def __enter__(self):
                events.append("enter")
                return started

            def __exit__(self, *_exc):
                events.append("exit")
                return False

        import playwright.sync_api

        monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _Manager())
        if hasattr(browser_pool._local, "playwright"):
            del browser_pool._local.playwright

        with discovery._playwright_for_this_thread() as playwright:
            assert playwright is started

        assert events == ["enter", "exit"], "the private instance was not closed"


class TestThePoolSlotIsPerThread:
    def test_another_threads_instance_is_never_borrowed(self, clean_pool):
        """`threading.local`, not a process global. Borrowing across threads is
        the OTHER Playwright failure -- "Cannot switch to a different thread" --
        and swapping one for the other would be no progress at all."""
        browser_pool._local.playwright = _FakePlaywright()
        seen: list[object] = []

        def _in_another_thread() -> None:
            seen.append(browser_pool.current_playwright)

        thread = threading.Thread(target=_in_another_thread)
        thread.start()
        thread.join()

        assert seen == [None]
