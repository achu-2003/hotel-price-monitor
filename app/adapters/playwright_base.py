"""Shared Playwright machinery: browser lifecycle, pinning, and safety rails.

Runs on the SYNC Playwright API. Celery prefork workers have no event loop, and
the async API inside a worker is a well-known source of silent hangs.

What this module guarantees for every fetch:

* **Deterministic conditions.** Locale, timezone, viewport, colour scheme and
  currency are pinned. Hotel prices vary by visitor geography, device and
  session, so without pinning we would be measuring our own variation.
* **A fresh context per fetch.** Contexts are cheap (~50ms); browsers are not.
  Reusing a context across hotels would let cookies from one hotel change the
  price shown by the next.
* **Politeness.** Images, fonts, media and analytics are blocked — roughly 60%
  faster for us and substantially less load on their servers.
* **Honesty.** The User-Agent identifies this tool and carries a contact URL.
  We do not disguise ourselves.
* **A hard stop on refusal.** If a bot wall or CAPTCHA appears we raise
  ``BlockedError`` and give up. Nothing here solves a challenge.

  The one exception to "honesty" above is ``as_visitor``: a site the owner
  has decided to read the way a guest's browser does (Goibibo, see
  app/adapters/goibibo.py) gets installed Chrome, its own user agent and every
  resource. That is chosen per ADAPTER, never per hotel, and the source still
  needs its ToS review recorded before anything fetches it.
* **Evidence on failure.** A screenshot and the HTML are saved so a broken
  selector is a ten-minute fix instead of a two-hour investigation.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from app.config import get_settings
from app.core.errors import (
    BlockedError,
    BrowserCrashError,
    FetchError,
    HttpStatusError,
    NetworkError,
    TimeoutError_,
)
from app.core.logging import get_logger

log = get_logger("playwright")

# Resource types that cost bandwidth and time but never carry a price.
#: Chromium net errors that mean "the host never answered". Reported as a
#: plain PlaywrightError, so they need naming to be classified honestly.
_TIMEOUT_NET_ERRORS = ("err_timed_out", "err_connection_timed_out")

_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_FRAGMENTS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
    "hotjar", "clarity.ms", "segment.io", "mixpanel", "intercom",
    "criteo", "taboola", "outbrain", "adservice",
)

# Text that means "we do not want automated visitors". Seeing any of these is
# a full stop, not a puzzle to solve.
_BOT_WALL_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "cf-challenge", "cloudflare",
    "are you a robot", "are you a human", "unusual traffic",
    "access denied", "verify you are human", "bot detection",
    "please enable javascript and cookies",
)


@dataclass
class CapturedResponse:
    """A JSON response seen while the page loaded."""

    url: str
    status: int
    payload: Any


@dataclass
class BrowserFetch:
    """Handle passed to adapters: the page plus everything captured alongside it."""

    page: Page
    json_responses: list[CapturedResponse] = field(default_factory=list)

    def find_json(self, *url_fragments: str) -> Any | None:
        """The last JSON response whose URL contains any fragment.

        Preferring a booking engine's own availability endpoint over the DOM is
        the single highest-leverage decision in this whole layer: JSON survives
        redesigns, CSS selectors do not.
        """
        for captured in reversed(self.json_responses):
            if any(frag in captured.url for frag in url_fragments):
                return captured.payload
        return None


def build_user_agent(suffix: str) -> str:
    """A real Chrome UA with our identifier appended.

    We keep the real browser token because sites legitimately use it for
    rendering decisions, and we append our own name and contact URL so any
    operator inspecting their logs can see exactly who we are and reach us.
    """
    base = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    return f"{base} {suffix}".strip()


def _install_resource_blocking(context: BrowserContext) -> None:
    def _route(route, request) -> None:
        try:
            if request.resource_type in _BLOCKED_RESOURCE_TYPES:
                route.abort()
                return
            url = request.url.lower()
            if any(fragment in url for fragment in _BLOCKED_URL_FRAGMENTS):
                route.abort()
                return
            route.continue_()
        except PlaywrightError:
            # The page can navigate away mid-route; losing one request is fine.
            pass

    context.route("**/*", _route)


def _install_json_capture(page: Page, sink: list[CapturedResponse]) -> None:
    def _on_response(response: Response) -> None:
        try:
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                return
            if response.status >= 400:
                return
            sink.append(
                CapturedResponse(url=response.url, status=response.status, payload=response.json())
            )
        except Exception:  # noqa: BLE001 - a body we cannot read is not worth failing over
            pass

    page.on("response", _on_response)


def detect_bot_wall(page: Page) -> str | None:
    """Return the marker found, or ``None``.

    Checked on the title and a bounded slice of body text: a full-page scan on
    every fetch would be wasteful, and bot walls announce themselves early.
    """
    try:
        title = (page.title() or "").lower()
        for marker in _BOT_WALL_MARKERS:
            if marker in title:
                return marker
        body = (page.inner_text("body", timeout=2_000) or "")[:3_000].lower()
        for marker in _BOT_WALL_MARKERS:
            if marker in body:
                return marker
    except PlaywrightError:
        return None
    return None


def _ensure_windows_subprocess_support() -> None:
    """Make sure this process can spawn a subprocess, on Windows.

    Playwright drives the browser through a Node process it starts itself.
    Spawning that needs asyncio's **Proactor** event loop; the **Selector**
    loop has no subprocess support on Windows and raises ``NotImplementedError``
    — with an empty message, which makes it maddening to diagnose.

    Celery installs the Selector policy on Windows, so a fetch that works when
    run directly fails the moment the same code runs inside a worker. That is
    exactly how this was found: identical adapter, identical config, success
    inline and a blank "unknown" error under the scheduler.

    A no-op everywhere else, including the Linux containers used in production,
    where Proactor does not exist and the default policy is already correct.
    """
    if sys.platform != "win32":
        return

    import asyncio

    proactor = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if proactor is None:
        return
    if not isinstance(asyncio.get_event_loop_policy(), proactor):
        asyncio.set_event_loop_policy(proactor())
        log.info("asyncio_policy_switched_to_proactor",
                 reason="Playwright needs subprocess support on Windows")


#: Every context's window. Pinned: prices vary by device, so a moving viewport
#: would be measuring ourselves.
_VIEWPORT = {"width": 1366, "height": 900}

#: How long a freshly started Chrome has to open its DevTools port.
_DEVTOOLS_WAIT_SECONDS = 30


def _visitor_chrome_executable(channel: str, playwright) -> str:
    """The installed Chrome for ``channel``, or the bundled Chromium for "".

    Found by its standard install path because a Chrome we start ourselves is
    not located by Playwright. ``playwright install chrome`` puts it at
    /opt/google/chrome on Linux, which is where the browser image has it.
    """
    if not channel:
        return playwright.chromium.executable_path
    if channel != "chrome":
        raise RuntimeError(
            f"Unsupported browser_visitor_channel {channel!r}; use 'chrome' or ''."
        )
    candidates: list[str] = []
    if sys.platform == "win32":
        for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(root):
                candidates.append(
                    os.path.join(os.environ[root], "Google", "Chrome", "Application", "chrome.exe")
                )
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        candidates += [
            "/opt/google/chrome/chrome",
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
        ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError(
        "Google Chrome is not installed (looked in: "
        + ", ".join(c for c in candidates if c) + ")."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_devtools(port: int, process: subprocess.Popen) -> None:
    """Return once Chrome answers on its DevTools port; raise if it never does."""
    deadline = time.monotonic() + _DEVTOOLS_WAIT_SECONDS
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Chrome exited with code {process.returncode} before opening DevTools."
            )
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(
        f"Chrome did not open DevTools on port {port} within {_DEVTOOLS_WAIT_SECONDS}s."
    )


def _die_with_parent() -> dict[str, Any]:
    """Popen options that end Chrome if the worker dies without closing it.

    The shutdown signal closes the pool on a normal recycle; this covers a
    worker that is killed outright. Linux only: PR_SET_PDEATHSIG has no
    Windows twin, and there the worker runs in a session somebody can see.
    """
    if not sys.platform.startswith("linux"):
        return {}

    def _set_pdeathsig() -> None:
        import ctypes
        import signal

        pr_set_pdeathsig = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(pr_set_pdeathsig, signal.SIGTERM)

    return {"preexec_fn": _set_pdeathsig}


class BrowserPool:
    """One browser per worker process, many contexts.

    Celery prefork gives each worker child its own process, so a module-level
    instance is naturally per-process. ``--max-tasks-per-child`` recycles the
    process periodically because Chromium leaks memory over hundreds of runs.
    """

    def __init__(self) -> None:
        # PER THREAD, not per process.
        #
        # Playwright's sync API is greenlet-based and bound to the thread that
        # created it; touching it from another raises "Cannot switch to a
        # different thread". A single shared browser looks fine until the pool
        # hands a task to a different thread, and then every browser fetch
        # fails with an error that says nothing about threads being the cause.
        #
        # threading.local gives each thread its own Playwright and browser, so
        # the pool is safe wherever it is used — Celery's solo pool, a prefork
        # child, or a web request handed to a threadpool.
        self._local = threading.local()

    @property
    def _playwright(self):
        return getattr(self._local, "playwright", None)

    @property
    def current_playwright(self):
        """This thread's live Playwright instance, or ``None``.

        Public because anything else in this thread that wants a browser has to
        reuse it rather than start a second. The pool never stops what it
        starts -- one browser per worker for the process lifetime is the whole
        point -- so from the first fetch onwards this thread permanently has a
        sync Playwright with a running event loop underneath it. A second
        ``sync_playwright()`` in that state raises "It looks like you are using
        Playwright Sync API inside the asyncio loop", which reads as a coding
        error in the caller and is really just this instance still being alive.
        """
        return self._playwright

    @property
    def _browser(self) -> Browser | None:
        return getattr(self._local, "browser", None)

    def _ensure_browser(self) -> Browser:
        browser = self._browser
        if browser is not None and browser.is_connected():
            return browser

        settings = get_settings()
        if self._playwright is None:
            _ensure_windows_subprocess_support()
            self._local.playwright = sync_playwright().start()

        self._local.browser = self._local.playwright.chromium.launch(
            headless=settings.browser_headless,
            args=[
                "--disable-dev-shm-usage",   # /dev/shm is tiny in containers
                "--no-sandbox",              # required unrestricted in most images
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-extensions",
            ],
        )
        return self._local.browser

    def _ensure_visitor_browser(self, locale: str, timezone: str) -> Browser:
        """The installed Chrome, for ``as_visitor`` contexts. One per thread too.

        A second browser rather than a flag on the first, because the two
        cannot be one process: the scraper's is headless Chromium, this one is
        a Chrome window. Started on first use, so a worker that never reads a
        visitor site never opens a window.

        Locale and timezone belong to the Chrome itself (``--lang`` and ``TZ``)
        rather than to a context's emulation, so the thread's first visitor
        fetch sets them for the browser's lifetime. Every hotel pins the same
        pair today.
        """
        browser = getattr(self._local, "visitor_browser", None)
        if browser is not None and browser.is_connected():
            return browser
        # A browser that disconnected took nothing with it: the Chrome it was
        # attached to may still be running, and is ended before another starts.
        self._stop_visitor_chrome()

        settings = get_settings()
        if self._playwright is None:
            _ensure_windows_subprocess_support()
            self._local.playwright = sync_playwright().start()

        # STARTED BY US AND ATTACHED TO, NOT LAUNCHED BY PLAYWRIGHT.
        #
        # From the morning of 5 Nov 2026 Goibibo answered every load from a
        # Playwright-launched Chrome with its "200 - OK" refusal, on the server
        # and on a home connection alike, while the same installed Chrome on
        # the same connection, started as an ordinary program and attached to
        # over DevTools, was served the room list. Alternated load by load:
        # launched 0 of ~12, attached 5 of 7 -- the two misses minutes after
        # other loads, which is the edge's frequency scoring. Dropping
        # --enable-automation or adding
        # --disable-blink-features=AutomationControlled to a launch did not
        # help, so it is not one flag; it is the launch.
        #
        # None of the scraper's launch flags either. --disable-gpu and
        # --disable-extensions are exactly the differences from an ordinary
        # Chrome that a bot score is built from.
        executable = _visitor_chrome_executable(
            settings.browser_visitor_channel, self._local.playwright
        )
        profile = tempfile.mkdtemp(prefix="hpm-visitor-")
        # --lang alone is ignored on Windows, where the language comes from the
        # profile; written into the throwaway profile it holds everywhere.
        os.makedirs(os.path.join(profile, "Default"))
        with open(os.path.join(profile, "Default", "Preferences"), "w", encoding="utf-8") as prefs:
            json.dump({"intl": {"accept_languages": f"{locale},{locale.split('-')[0]}"}}, prefs)
        port = _free_port()
        args = [
            executable,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
            f"--lang={locale}",
        ]
        if settings.browser_visitor_headless:
            args.append("--headless=new")
        if sys.platform.startswith("linux"):
            # The container's Chrome has never had a sandbox: Playwright's own
            # launch passes --no-sandbox by default, and an unprivileged user
            # in Docker cannot create the namespaces the sandbox needs. A small
            # /dev/shm is handled by the compose file's shm_size.
            args.append("--no-sandbox")
        process = subprocess.Popen(
            args,
            env={**os.environ, "TZ": timezone, "LANGUAGE": locale.replace("-", "_")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_die_with_parent(),
        )
        self._local.visitor_process = process
        self._local.visitor_profile = profile
        try:
            _wait_for_devtools(port, process)
            self._local.visitor_browser = self._local.playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        except Exception:
            self._stop_visitor_chrome()
            raise
        log.info("visitor_chrome_started", pid=process.pid, port=port)
        return self._local.visitor_browser

    def _stop_visitor_chrome(self) -> None:
        """End the Chrome this thread started, and delete its throwaway profile."""
        process = getattr(self._local, "visitor_process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self._local.visitor_process = None
        profile = getattr(self._local, "visitor_profile", None)
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
        self._local.visitor_profile = None

    @contextmanager
    def context(
        self,
        *,
        locale: str,
        timezone: str,
        as_person: bool = False,
        as_visitor: bool = False,
        storage_state: dict | None = None,
    ) -> Generator[BrowserContext, None, None]:
        """A fresh context. ``as_person`` makes it look like an ordinary browser.

        The default is the scraper's: a user agent that names this monitor,
        so a site can tell who is reading it, and no images, fonts or media,
        because a price page is read for its numbers. Both are wrong for
        SIGNING IN AS THE OWNER to their own application. That is not a
        scrape, it is the owner's browser with the owner's password in it,
        and it should look like one: RMS Cloud answered the announced user
        agent with "Object reference not set to an instance of an object"
        where a plain Chrome string got an ordinary login.

        ``as_visitor`` goes one further, for a site that serves its rooms only
        to a browser indistinguishable from a guest's: the installed Chrome,
        with ITS OWN user agent. Overriding it is a tell -- the string says
        Chrome 131 on Linux while the browser's client hints say otherwise.
        Nothing is blocked either: on 26 Oct 2026 Goibibo answered every load
        with an empty room list once images were turned off, and with the
        full list when they were not.
        """
        settings = get_settings()
        if as_visitor:
            # A PLAIN context: no locale, timezone or viewport overrides. The
            # first two are set on the Chrome itself (_ensure_visitor_browser).
            # On 5 Nov 2026 the plain context of an attached Chrome is what was
            # served; given all three overrides it was refused both times.
            ctx = self._ensure_visitor_browser(locale, timezone).new_context(
                storage_state=storage_state,
            )
        else:
            ctx = self._ensure_browser().new_context(
                locale=locale,
                timezone_id=timezone,
                viewport=_VIEWPORT,
                color_scheme="light",
                java_script_enabled=True,
                ignore_https_errors=False,
                # Cookies and local storage from an earlier visit: how a site
                # that trusted this "device" once recognises it again.
                storage_state=storage_state,
                user_agent=build_user_agent(
                    "" if as_person else settings.browser_user_agent_suffix
                ),
            )
        ctx.set_default_timeout(settings.browser_nav_timeout_ms)
        ctx.set_default_navigation_timeout(settings.browser_nav_timeout_ms)
        if not (as_person or as_visitor):
            _install_resource_blocking(ctx)
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except PlaywrightError:
                pass

    def close(self) -> None:
        """Shut down THIS thread's browsers.

        Only the calling thread's instances are touched: closing another
        thread's browser from here would be the very cross-thread access this
        design exists to avoid.
        """
        for attr in ("browser", "visitor_browser"):
            try:
                browser = getattr(self._local, attr, None)
                if browser is not None:
                    browser.close()
            except PlaywrightError:
                pass
            setattr(self._local, attr, None)
        # Closing an ATTACHED browser only disconnects from it; the Chrome
        # process is ours to end.
        self._stop_visitor_chrome()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._local.playwright = None


#: Module-level, therefore one per Celery worker process.
browser_pool = BrowserPool()


def save_artifacts(page: Page, label: str) -> tuple[str | None, str | None]:
    """Screenshot + HTML for a failed fetch.

    Never raises: losing the evidence must not mask the original error.
    """
    settings = get_settings()
    try:
        directory = Path(settings.artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{label}-{uuid.uuid4().hex[:8]}"
        shot = directory / f"{stem}.png"
        html = directory / f"{stem}.html"
        page.screenshot(path=str(shot), full_page=False)
        html.write_text(page.content(), encoding="utf-8")
        return str(shot), str(html)
    except Exception as exc:  # noqa: BLE001
        log.warning("artifact_capture_failed", error=str(exc))
        return None, None


@contextmanager
def open_page(
    url: str,
    *,
    locale: str = "en-IN",
    timezone: str = "Asia/Kolkata",
    wait_until: str = "domcontentloaded",
    artifact_label: str = "fetch",
    as_person: bool = False,
    as_visitor: bool = False,
    storage_state: dict | None = None,
) -> Generator[BrowserFetch, None, None]:
    """Open ``url`` in a fresh, pinned context and hand back the page.

    Translates Playwright's exceptions into the project's error taxonomy so the
    retry policy can be applied consistently, and captures artifacts on the way
    out when something goes wrong.
    """
    settings = get_settings()
    started = time.monotonic()

    with browser_pool.context(
        locale=locale, timezone=timezone, as_person=as_person, as_visitor=as_visitor,
        storage_state=storage_state,
    ) as ctx:
        page = ctx.new_page()
        captured: list[CapturedResponse] = []
        _install_json_capture(page, captured)

        try:
            response = page.goto(
                url, wait_until=wait_until, timeout=settings.browser_nav_timeout_ms
            )
            # THE STATUS, BEFORE ANYBODY PARSES THE BODY. Chromium renders a
            # 502 as happily as a hotel page, so without this the site's error
            # page reaches the adapter and its selectors match nothing -- which
            # is then reported as a redesign, alerts somebody, and spends a
            # rediscovery attempt on a page that never had a room table.
            #
            # Here rather than in one adapter because every adapter navigates
            # through this function, and a site having a bad minute is not a
            # fact about the adapter that happened to ask.
            #
            # 5xx ONLY. A 4xx is ours to fix -- a wrong URL, a dropped
            # affiliate parameter, a page that has genuinely gone -- and
            # retrying it three times would hammer the site and hide it. It
            # falls through to the parse-stage checks, which say what was on
            # the page rather than guessing from a number.
            if response is not None and response.status >= 500:
                raise HttpStatusError(
                    f"{response.status} from {url}",
                    status_code=response.status,
                    context={"url": url},
                )
        except PlaywrightTimeout as exc:
            raise TimeoutError_(
                f"Timed out loading {url}", context={"url": url}
            ) from exc
        except PlaywrightError as exc:
            message = str(exc).lower()
            if "closed" in message or "crashed" in message:
                raise BrowserCrashError(str(exc), context={"url": url}) from exc
            # Chromium gives up on an unreachable host by its own internal
            # connect timeout, well before browser_nav_timeout_ms, and reports
            # it as a plain Error carrying net::ERR_TIMED_OUT -- not as a
            # PlaywrightTimeout. Left to fall through, a timeout was recorded
            # as class `network` and retried three times instead of two, so an
            # unreachable host was hit harder than a slow one and the
            # Attention page named the wrong cause.
            if any(marker in message for marker in _TIMEOUT_NET_ERRORS):
                raise TimeoutError_(
                    f"Timed out loading {url}", context={"url": url}
                ) from exc
            raise NetworkError(str(exc), context={"url": url}) from exc

        # Refusal check comes first: everything after it would be parsing a
        # challenge page and reporting nonsense.
        if marker := detect_bot_wall(page):
            shot, html = save_artifacts(page, f"blocked-{artifact_label}")
            log.warning("bot_wall_detected", url=url, marker=marker)
            raise BlockedError(
                f"Automated access is not welcome at {url} (matched {marker!r}). "
                f"Stopping; this source needs a human decision, not a workaround.",
                context={"url": url, "marker": marker,
                         "screenshot_path": shot, "html_path": html},
            )

        try:
            yield BrowserFetch(page=page, json_responses=captured)
        except Exception as exc:
            shot, html = save_artifacts(page, f"error-{artifact_label}")
            # The paths go ONTO the error, not only into the log.
            #
            # ``record_error`` fills the screenshot_path and html_path columns
            # from the error's context, and the only place that ever set them
            # was the bot-wall branch twenty lines up. So every drift, every
            # config failure, every crash wrote a screenshot and an HTML dump
            # to disk -- and then the Attention page told whoever opened it
            # "No screenshot or HTML was captured. Either this failed before
            # the page loaded, or the artifacts have been pruned." Both halves
            # untrue, about evidence sitting on disk the whole time, in a
            # message that reads as a dead end rather than a plumbing gap.
            #
            # ``setdefault`` because an adapter that already recorded its own
            # artifacts knows better than this generic handler does.
            if isinstance(exc, FetchError):
                if shot is not None:
                    exc.context.setdefault("screenshot_path", shot)
                if html is not None:
                    exc.context.setdefault("html_path", html)
            log.warning(
                "fetch_failed_artifacts_saved",
                url=url, screenshot_path=shot, html_path=html,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        finally:
            try:
                page.close()
            except PlaywrightError:
                pass
