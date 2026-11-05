"""The visitor browser is a Chrome we start and attach to, not one Playwright launches.

THE FAULT THIS PINS
===================
From the morning of 5 Nov 2026 Goibibo answered every load from a
Playwright-launched Chrome with its bare "200 - OK" refusal, on the server and
on a home connection alike. Every Goibibo check failed, three circuit breakers
opened and Attention filled with 48 "sent no room list" rows. The same Chrome on
the same connection, started as an ordinary program and attached to over
DevTools, was served the room list.

So these tests fix three things: the visitor browser is never
``chromium.launch``-ed, its context carries none of the emulation overrides the
scraper's does, and shutting the pool down ends the Chrome process (closing an
attached browser only disconnects) and deletes its throwaway profile.

No browser is started here. What is being tested is how one would be.
"""
from __future__ import annotations

import json
import os

import pytest

from app.adapters import playwright_base
from app.adapters.playwright_base import BrowserPool


class _FakeProcess:
    def __init__(self, args, env=None, **_kwargs) -> None:
        self.args = args
        self.env = env
        self.pid = 4242
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - only if terminate hangs
        self.returncode = -9


class _FakeContext:
    def set_default_timeout(self, _ms) -> None: ...
    def set_default_navigation_timeout(self, _ms) -> None: ...
    def route(self, *_a) -> None:  # pragma: no cover - visitor contexts are never routed
        raise AssertionError("a visitor context had resources blocked")
    def close(self) -> None: ...


class _FakeBrowser:
    def __init__(self) -> None:
        self.context_kwargs: list[dict] = []
        self.closed = False

    def is_connected(self) -> bool:
        return not self.closed

    def new_context(self, **kwargs):
        self.context_kwargs.append(kwargs)
        return _FakeContext()

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    executable_path = "/bundled/chromium"

    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser
        self.cdp_urls: list[str] = []

    def launch(self, **_kwargs):
        raise AssertionError("the visitor Chrome was launched by Playwright")

    def connect_over_cdp(self, url: str):
        self.cdp_urls.append(url)
        return self.browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)

    def stop(self) -> None: ...


@pytest.fixture
def started(monkeypatch):
    """A pool whose visitor Chrome is fake, plus what was asked of it."""
    processes: list[_FakeProcess] = []

    def _popen(args, **kwargs):
        processes.append(_FakeProcess(args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(playwright_base.subprocess, "Popen", _popen)
    monkeypatch.setattr(playwright_base, "_wait_for_devtools", lambda port, process: None)
    monkeypatch.setattr(
        playwright_base, "_visitor_chrome_executable", lambda channel, pw: "/opt/google/chrome/chrome"
    )

    browser = _FakeBrowser()
    pool = BrowserPool()
    pool._local.playwright = _FakePlaywright(browser)
    return pool, browser, processes


def test_the_visitor_chrome_is_started_and_attached_to(started):
    pool, browser, processes = started

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass

    assert len(processes) == 1
    args = processes[0].args
    assert args[0] == "/opt/google/chrome/chrome"
    port = next(a.split("=", 1)[1] for a in args if a.startswith("--remote-debugging-port="))
    assert pool._local.playwright.chromium.cdp_urls == [f"http://127.0.0.1:{port}"]
    assert "--enable-automation" not in args
    assert processes[0].env["TZ"] == "Asia/Kolkata"


def test_the_visitor_context_carries_no_emulation(started):
    pool, browser, _ = started

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass

    (kwargs,) = browser.context_kwargs
    assert set(kwargs) == {"storage_state"}


def test_the_locale_is_written_into_the_profile(started):
    pool, _, processes = started

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        profile = next(
            a.split("=", 1)[1] for a in processes[0].args if a.startswith("--user-data-dir=")
        )
        with open(os.path.join(profile, "Default", "Preferences"), encoding="utf-8") as prefs:
            assert json.load(prefs)["intl"]["accept_languages"].startswith("en-IN")
    pool.close()


def test_one_chrome_serves_every_fetch_of_a_thread(started):
    pool, _, processes = started

    for _ in range(3):
        with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
            pass

    assert len(processes) == 1


def test_closing_the_pool_ends_chrome_and_deletes_its_profile(started):
    pool, browser, processes = started

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass
    profile = pool._local.visitor_profile
    assert os.path.isdir(profile)

    pool.close()

    assert browser.closed
    assert processes[0].terminated
    assert not os.path.exists(profile)


def test_a_dead_chrome_is_replaced_not_reused(started):
    pool, browser, processes = started

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass
    browser.closed = True  # Chrome crashed: the attachment is gone.

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass

    assert len(processes) == 2
    assert processes[0].terminated
    pool.close()


def test_a_chrome_that_never_opens_devtools_is_not_left_running(started, monkeypatch):
    pool, _, processes = started

    def _never(port, process):
        raise RuntimeError("Chrome did not open DevTools")

    monkeypatch.setattr(playwright_base, "_wait_for_devtools", _never)

    with pytest.raises(RuntimeError):
        with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
            pass

    assert processes[0].terminated
    assert pool._local.visitor_profile is None


def test_wait_for_devtools_reports_a_chrome_that_exited():
    process = _FakeProcess(["chrome"])
    process.returncode = 1
    with pytest.raises(RuntimeError, match="exited with code 1"):
        playwright_base._wait_for_devtools(1, process)


def test_an_unknown_channel_is_refused_not_guessed():
    with pytest.raises(RuntimeError, match="Unsupported"):
        playwright_base._visitor_chrome_executable("msedge", None)


def test_no_channel_means_the_bundled_chromium():
    class _PW:
        class chromium:
            executable_path = "/bundled/chromium"

    assert playwright_base._visitor_chrome_executable("", _PW) == "/bundled/chromium"

