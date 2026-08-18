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


class BrowserPool:
    """One browser per worker process, many contexts.

    Celery prefork gives each worker child its own process, so a module-level
    instance is naturally per-process. ``--max-tasks-per-child`` recycles the
    process periodically because Chromium leaks memory over hundreds of runs.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None

    def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        settings = get_settings()
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        self._browser = self._playwright.chromium.launch(
            headless=settings.browser_headless,
            args=[
                "--disable-dev-shm-usage",   # /dev/shm is tiny in containers
                "--no-sandbox",              # required unrestricted in most images
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-extensions",
            ],
        )
        return self._browser

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
        try:
            if self._browser is not None:
                self._browser.close()
        except PlaywrightError:
            pass
        finally:
            self._browser = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None


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
