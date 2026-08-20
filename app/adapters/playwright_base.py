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
  ``BlockedError`` and give up. There is no evasion path in this codebase by
  design, and adding one would violate the project's stated boundaries.
* **Evidence on failure.** A screenshot and the HTML are saved so a broken
  selector is a ten-minute fix instead of a two-hour investigation.
"""
from __future__ import annotations

import sys
import threading
import time
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
    NetworkError,
    TimeoutError_,
)
from app.core.logging import get_logger

log = get_logger("playwright")

# Resource types that cost bandwidth and time but never carry a price.
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

    @contextmanager
    def context(self, *, locale: str, timezone: str) -> Generator[BrowserContext, None, None]:
        settings = get_settings()
        browser = self._ensure_browser()
        ctx = browser.new_context(
            locale=locale,
            timezone_id=timezone,
            viewport={"width": 1366, "height": 900},
            user_agent=build_user_agent(settings.browser_user_agent_suffix),
            color_scheme="light",
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        ctx.set_default_timeout(settings.browser_nav_timeout_ms)
        ctx.set_default_navigation_timeout(settings.browser_nav_timeout_ms)
        _install_resource_blocking(ctx)
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except PlaywrightError:
                pass

    def close(self) -> None:
        """Shut down THIS thread's browser.

        Only the calling thread's instance is touched: closing another
        thread's browser from here would be the very cross-thread access this
        design exists to avoid.
        """
        try:
            if self._browser is not None:
                self._browser.close()
        except PlaywrightError:
            pass
        finally:
            self._local.browser = None
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
) -> Generator[BrowserFetch, None, None]:
    """Open ``url`` in a fresh, pinned context and hand back the page.

    Translates Playwright's exceptions into the project's error taxonomy so the
    retry policy can be applied consistently, and captures artifacts on the way
    out when something goes wrong.
    """
    settings = get_settings()
    started = time.monotonic()

    with browser_pool.context(locale=locale, timezone=timezone) as ctx:
        page = ctx.new_page()
        captured: list[CapturedResponse] = []
        _install_json_capture(page, captured)

        try:
            page.goto(url, wait_until=wait_until, timeout=settings.browser_nav_timeout_ms)
        except PlaywrightTimeout as exc:
            raise TimeoutError_(
                f"Timed out loading {url}", context={"url": url}
            ) from exc
        except PlaywrightError as exc:
            message = str(exc).lower()
            if "closed" in message or "crashed" in message:
                raise BrowserCrashError(str(exc), context={"url": url}) from exc
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
        except Exception:
            shot, html = save_artifacts(page, f"error-{artifact_label}")
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
