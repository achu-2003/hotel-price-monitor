#!/usr/bin/env python
"""Read the sites that refuse the server's address, from this PC.

Goibibo shows the production server only its "Sold Out" page and shows a home
or office connection every room. This runs on such a connection: it asks the
server which page is due, loads it in Chrome here with the same adapter the
server would have used, and posts the prices back. The server does the rest
(app/services/remote_reader.py explains the arrangement).

    .venv312\\Scripts\\python.exe scripts\\remote_reader.py           # keep running
    .venv312\\Scripts\\python.exe scripts\\remote_reader.py --once    # one page, then stop

Needs, in .env on this PC:
    REMOTE_READER_URL   the server's address, e.g. https://hpm.example
    REMOTE_READER_KEY   the same value the server was given

No database and no Redis are needed here. It only ever CALLS the server, so
nothing has to be opened on this PC's router. While it runs the server leaves
these sites to it; a few minutes after it stops, the server reads them itself
again. scripts\\install-reader.ps1 starts it at every sign-in.

One page a minute at most, one at a time -- the pace the site accepts.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.adapters import registry  # noqa: E402
from app.adapters.base import FetchContext  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.errors import classify  # noqa: E402
from app.services.dates import StayWindow  # noqa: E402
from app.services.remote_reader import result_to_json  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

#: Between two page loads. The site is read one page a minute by the server
#: too; the spread keeps the loads from landing on the same second each time.
GAP_SECONDS = (60, 90)
#: After the server could not be reached, or refused the key.
RETRY_SECONDS = 30


def close_browsers() -> None:
    """Close the Chrome kept open between pages.

    The browser pool keeps one Chrome for the life of the process, which is
    right for a reader that runs all day. On Windows nothing ends that Chrome
    when this process does (playwright_base._die_with_parent is Linux only),
    so without this every stop and restart of the reader would leave another
    window behind. The Celery worker does the same on shutdown.
    """
    try:
        from app.adapters.playwright_base import browser_pool

        browser_pool.close()
    except Exception as exc:  # noqa: BLE001 - on the way out; nothing to do about it
        say(f"could not close the browser: {type(exc).__name__}")


def say(text: str) -> None:
    print(f"{datetime.now():%d %b %H:%M:%S}  {text}", flush=True)


def read(job: dict, settings) -> dict:
    """Load one page. Returns what to post back: a result, or an error."""
    payload = job["payload"]
    context = FetchContext(
        hotel_source_id=payload["hotel_source_id"], hotel_name=payload["hotel_name"],
        url=payload["url"], external_id=payload["external_id"],
        stay=StayWindow(check_in=date.fromisoformat(payload["check_in"][:10]),
                        check_out=date.fromisoformat(payload["check_out"][:10])),
        adults=payload["adults"], children=payload["children"], rooms=payload["rooms"],
        currency=payload["currency"], locale=settings.browser_locale,
        timezone=settings.browser_timezone, config=payload["adapter_config"] or {},
        check_run_id=job["check_run_id"],
    )
    try:
        result = registry.get_adapter(payload["adapter_key"]).fetch(context)
    except Exception as exc:  # noqa: BLE001 - reported to the server, never fatal here
        error = classify(exc)
        say(f"   failed: [{error.error_class}] {str(error)[:200]}")
        return {"error_class": str(error.error_class), "error": str(error)[:2000]}
    on_sale = sum(1 for offer in result.offers if offer.is_available)
    say(f"   read {len(result.offers)} prices, {on_sale} on sale")
    return result_to_json(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="read one page (or find none) and stop")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.remote_reader_url or settings.remote_reader_key is None:
        say("REMOTE_READER_URL and REMOTE_READER_KEY must both be set in .env on this PC.")
        return 2
    base = settings.remote_reader_url.rstrip("/") + "/api/v1/reader"
    headers = {"X-Reader-Key": settings.remote_reader_key.get_secret_value()}
    say(f"reader started, collecting from {settings.remote_reader_url}")

    quiet = False  # one line when the queue goes empty, not one per poll
    with httpx.Client(timeout=60, headers=headers) as http:
        while True:
            try:
                answer = http.post(f"{base}/next")
                if answer.status_code in (401, 404):
                    why = ("the server has no REMOTE_READER_KEY set" if answer.status_code == 404
                           else "REMOTE_READER_KEY here does not match the server's")
                    say(f"refused ({answer.status_code}): {why}")
                    if args.once:
                        return 1
                    time.sleep(RETRY_SECONDS * 4)
                    continue
                answer.raise_for_status()
                body = answer.json()
            except (httpx.HTTPError, ValueError) as exc:
                say(f"server not reachable: {type(exc).__name__} {str(exc)[:120]}")
                if args.once:
                    return 1
                time.sleep(RETRY_SECONDS)
                continue

            job = body.get("job")
            if job is None:
                if args.once:
                    say("nothing waiting")
                    return 0
                if not quiet:
                    say("nothing waiting; asking again every "
                        f"{body.get('wait_seconds', 20)} s")
                    quiet = True
                time.sleep(float(body.get("wait_seconds") or 20))
                continue

            quiet = False
            payload = job["payload"]
            say(f"{payload['hotel_name']} · {payload['adapter_key']} · night of {payload['check_in'][:10]}")
            report = {"job_id": job["id"], **read(job, settings)}

            # The page is read; losing it to one dropped connection would waste
            # a load the site counts. Three tries, then the server gives up on
            # this check by itself and schedules the next.
            for attempt in range(3):
                try:
                    posted = http.post(f"{base}/result", json=report)
                    if posted.status_code == 410:
                        say("   the server had already given up on this check")
                    else:
                        posted.raise_for_status()
                        say(f"   recorded: {posted.json()}")
                    break
                except (httpx.HTTPError, ValueError) as exc:
                    say(f"   could not post the result ({type(exc).__name__}), try {attempt + 1} of 3")
                    time.sleep(10)

            if args.once:
                return 0
            time.sleep(random.uniform(*GAP_SECONDS))


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        say("reader stopped")
        code = 0
    finally:
        close_browsers()
    sys.exit(code)
