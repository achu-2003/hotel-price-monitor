#!/usr/bin/env python
"""Phase 0 reconnaissance: can we read prices from this site, and may we?

Run this against each of your ~30 hotels BEFORE any adapter is written. It
answers, per site:

  1. Does robots.txt permit us?          (a "no" ends it — we do not proceed)
  2. Is there a bot wall or CAPTCHA?     (also ends it)
  3. Does the page expose a JSON availability endpoint?
     ...if yes, that hotel gets the fast, stable HTTP adapter and will need
     almost no maintenance. This is the single best outcome and worth looking
     for on every site.
  4. What do the prices and room names look like in the DOM?
  5. It saves a screenshot and the HTML as a test fixture.

Usage
-----
    python scripts/probe_site.py https://somehotel.example/booking
    python scripts/probe_site.py --url https://... --check-in 2026-09-15 --nights 1
    python scripts/probe_site.py --file hotels.txt        # one URL per line

Output goes to the terminal and to docs/SOURCES.md as a table you can hand to
whoever reviews Terms of Service.

This script only READS public pages, at a human pace, one at a time.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# Allow running as a plain script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.parsing import looks_sold_out  # noqa: E402

PRICE_TEXT_RE = re.compile(r"(₹|Rs\.?|INR)\s?[\d,]{3,}", re.IGNORECASE)
ROOM_WORD_RE = re.compile(
    r"\b(deluxe|standard|superior|premium|executive|suite|cottage|villa|"
    r"tent|dormitory|family|double|twin|triple|single|studio|apartment)\b",
    re.IGNORECASE,
)
# URL fragments that usually indicate a booking engine's own data endpoint.
JSON_HINTS = (
    "avail", "rate", "price", "room", "inventory", "booking", "search",
    "quote", "tariff", "calendar",
)


@dataclass
class ProbeReport:
    url: str
    robots_allowed: bool | None = None
    robots_reason: str = ""
    crawl_delay: float | None = None
    blocked_marker: str | None = None
    page_title: str = ""
    json_endpoints: list[str] = field(default_factory=list)
    price_samples: list[str] = field(default_factory=list)
    room_samples: list[str] = field(default_factory=list)
    sold_out_detected: bool = False
    screenshot: str | None = None
    html_fixture: str | None = None
    error: str | None = None

    @property
    def verdict(self) -> str:
        if self.robots_allowed is False:
            return "DO NOT USE - robots.txt disallows"
        if self.blocked_marker:
            return "DO NOT USE - bot wall / CAPTCHA present"
        if self.error:
            return "FAILED - see error"
        if self.json_endpoints and self.price_samples:
            return "BEST - JSON endpoint available (use HttpJsonAdapter)"
        if self.price_samples:
            return "OK - DOM scraping viable (use PlaywrightDirectSiteAdapter)"
        if self.sold_out_detected:
            return "RETRY - page says sold out; probe another date"
        return "MANUAL ENTRY - no prices found on this page"

    @property
    def recommended_adapter(self) -> str:
        if self.robots_allowed is False or self.blocked_marker:
            return "none"
        if self.json_endpoints and self.price_samples:
            return "http_json"
        if self.price_samples:
            return "playwright_direct_site"
        return "manual_entry"


def probe(url: str, check_in: date, nights: int, out_dir: Path) -> ProbeReport:
    from app.adapters.playwright_base import (
        browser_pool,
        build_user_agent,
        detect_bot_wall,
    )
    from app.adapters.robots import RobotsChecker

    report = ProbeReport(url=url)
    user_agent = build_user_agent("HotelPriceMonitor-Probe/1.0")

    # ── 1. May we? Asked before anything is fetched. ──────────────────
    checker = RobotsChecker(user_agent=user_agent, cache=None, enabled=True)
    verdict = checker.check(url)
    report.robots_allowed = verdict.allowed
    report.robots_reason = verdict.reason
    report.crawl_delay = verdict.crawl_delay

    if not verdict.allowed:
        # Stop here. Not a technical obstacle to route around.
        return report

    # ── 2. Load the page ─────────────────────────────────────────────
    from playwright.sync_api import Error as PlaywrightError

    try:
        with browser_pool.context(locale="en-IN", timezone="Asia/Kolkata") as ctx:
            page = ctx.new_page()
            captured: list[str] = []

            def _on_response(response) -> None:
                try:
                    ctype = response.headers.get("content-type", "")
                    if "application/json" in ctype and response.status < 400:
                        captured.append(response.url)
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_response)
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2_500)  # let late XHRs settle

            report.page_title = (page.title() or "")[:120]

            if marker := detect_bot_wall(page):
                report.blocked_marker = marker
                return report

            body_text = page.inner_text("body", timeout=5_000) or ""
            report.sold_out_detected = looks_sold_out(body_text[:5_000])

            report.price_samples = list(
                dict.fromkeys(m.group() for m in PRICE_TEXT_RE.finditer(body_text))
            )[:12]
            report.room_samples = list(dict.fromkeys(
                line.strip()[:70]
                for line in body_text.splitlines()
                if ROOM_WORD_RE.search(line) and len(line.strip()) < 70
            ))[:12]

            report.json_endpoints = [
                u for u in dict.fromkeys(captured)
                if any(hint in u.lower() for hint in JSON_HINTS)
            ][:10]

            out_dir.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^a-z0-9]+", "-", url.lower())[:60].strip("-")
            shot = out_dir / f"{stem}.png"
            html = out_dir / f"{stem}.html"
            page.screenshot(path=str(shot), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            report.screenshot, report.html_fixture = str(shot), str(html)

    except PlaywrightError as exc:
        report.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    except Exception as exc:  # noqa: BLE001
        report.error = f"{type(exc).__name__}: {str(exc)[:200]}"

    return report


def print_report(r: ProbeReport) -> None:
    print(f"\n{'=' * 72}\n{r.url}\n{'=' * 72}")
    print(f"  verdict          : {r.verdict}")
    print(f"  adapter          : {r.recommended_adapter}")
    print(f"  robots.txt       : {'ALLOWED' if r.robots_allowed else 'DISALLOWED'}"
          f"  ({r.robots_reason})")
    if r.crawl_delay:
        print(f"  crawl-delay      : {r.crawl_delay}s  (we will honour this)")
    if r.blocked_marker:
        print(f"  bot wall         : {r.blocked_marker!r}  <-- stop, do not work around")
    if r.page_title:
        print(f"  title            : {r.page_title}")
    if r.error:
        print(f"  error            : {r.error}")
    if r.json_endpoints:
        print("  JSON endpoints   : (prefer these over DOM scraping)")
        for endpoint in r.json_endpoints:
            print(f"      - {endpoint[:110]}")
    if r.price_samples:
        print(f"  prices seen      : {', '.join(r.price_samples[:8])}")
    if r.room_samples:
        print("  room-ish lines   :")
        for room in r.room_samples[:8]:
            print(f"      - {room}")
    if r.sold_out_detected:
        print("  note             : page mentions sold out; try a different date")
    if r.screenshot:
        print(f"  screenshot       : {r.screenshot}")
        print(f"  html fixture     : {r.html_fixture}")


def write_sources_doc(reports: list[ProbeReport], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Source Feasibility (Phase 0)",
        "",
        "Generated by `scripts/probe_site.py`. Automated findings only:",
        "a human must still review each site's Terms of Service and record the",
        "outcome in `docs/TOS_REVIEW.md` before that source is enabled.",
        "",
        "| URL | Verdict | Adapter | robots.txt | JSON API | Prices found |",
        "|---|---|---|---|---|---|",
    ]
    for r in reports:
        robots = "allowed" if r.robots_allowed else "DISALLOWED"
        lines.append(
            f"| {r.url[:60]} | {r.verdict} | `{r.recommended_adapter}` | {robots} | "
            f"{'yes' if r.json_endpoints else 'no'} | {len(r.price_samples)} |"
        )
    lines += ["", "## Next steps", "",
              "1. Review ToS for every row not already ruled out.",
              "2. Rows marked BEST get an `http_json` adapter (cheap, stable).",
              "3. Rows marked OK get a `playwright_direct_site` adapter.",
              "4. Rows marked MANUAL ENTRY are entered by hand in the dashboard.",
              "5. Rows marked DO NOT USE are not automated. No workarounds.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", nargs="?", help="a single URL to probe")
    parser.add_argument("--file", type=Path, help="text file with one URL per line")
    parser.add_argument("--check-in", type=date.fromisoformat,
                        default=date.today() + timedelta(days=7))
    parser.add_argument("--nights", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/probe"))
    parser.add_argument("--doc", type=Path, default=Path("docs/SOURCES.md"))
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    urls: list[str] = []
    if args.url:
        urls.append(args.url)
    if args.file:
        urls += [
            line.strip() for line in args.file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not urls:
        parser.error("give a URL or --file")

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Playwright is not installed. Run:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    reports: list[ProbeReport] = []
    for url in urls:
        report = probe(url, args.check_in, args.nights, args.out)
        reports.append(report)
        if not args.json:
            print_report(report)

    if args.json:
        print(json.dumps([r.__dict__ for r in reports], indent=2, default=str))
    else:
        write_sources_doc(reports, args.doc)
        usable = sum(1 for r in reports if r.recommended_adapter not in ("none", "manual_entry"))
        print(f"\nSummary: {usable}/{len(reports)} sites look automatable.")

    from app.adapters.playwright_base import browser_pool
    browser_pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
