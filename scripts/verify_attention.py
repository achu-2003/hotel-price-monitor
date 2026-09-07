#!/usr/bin/env python
"""Check the two faults on the Attention screen against evidence, not opinion.

    .venv312/Scripts/python.exe scripts/verify_attention.py

Read-only. Nothing here writes to the database and nothing drives a browser,
so it is safe to run on the production box while it is serving.

WHAT EACH HALF PROVES
=====================
1. Whether the collision fix is not merely CHECKED OUT but LOADED. Code on
   disk decides nothing: the alert is raised inside the Celery task, and a
   worker started before the pull keeps running the gate it imported at boot.

2. Whether "no elements matched room_card" means the site was redesigned. The
   decisive test is not the message, it is the page the fetcher saved beside
   it: if the selector it says matched nothing is present in that HTML, the
   selector is fine and the fetch simply never had the rendered page.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import sync_session  # noqa: E402
from app.services.ingest import IngestSummary  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _find_saved_page(html_path: str | None) -> Path | None:
    """The artifact the fetcher saved, wherever this box actually keeps it.

    The stored path is whatever ARTIFACT_DIR was when the row was written, and
    that has been three different things: a POSIX '/data/artifacts' inherited
    from the Docker compose file, the same string on Windows (where it lands on
    whichever drive happened to be current), and the 'C:\hotel-monitor\artifacts'
    the native deploy sets. A row written under one of those is still worth
    reading on a box configured with another, so the basename is tried against
    the CURRENT setting last rather than the lookup giving up.
    """
    if not html_path:
        return None
    candidates = [
        Path(html_path),
        ROOT / html_path.lstrip("/\\"),
        Path(get_settings().artifact_dir) / Path(html_path.replace("\\", "/")).name,
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def rule(n: str) -> None:
    print()
    print("=" * 74)
    print(n)
    print("=" * 74)


# -- 1. Is the collision fix the code that is RUNNING? ----------------
rule("1. IS THE COLLISION FIX LOADED, NOT JUST CHECKED OUT?")

head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=ROOT,
                      capture_output=True, text=True).stdout.strip()
print(f"  checked out       : {head}")

ingest_py = ROOT / "app" / "services" / "ingest.py"
has_fix = "offers_collapsed >= 2" in ingest_py.read_text(encoding="utf-8")
print(f"  gate on disk      : {'NEW (>= 2)' if has_fix else 'OLD (> 0)  <-- git pull needed'}")

# The gate evaluated exactly as the alert's own numbers would drive it.
summary = IngestSummary(offers_seen=12, offers_matched=12, offers_unmatched=0,
                        change_ids=[], outcomes=[], offers_collapsed=1,
                        collapsed_names=[], offers_duplicated=0)
print(f"  would '1 of 12' alert? {summary.name_selector_looks_broken}   (fixed code says False)")

# A worker that booted before the file was written is running the old gate,
# whatever git says. uvicorn --reload does not cover Celery.
out = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process -Filter \"Name='celery.exe'\" | "
     "ForEach-Object { $_.CreationDate.ToString('yyyy-MM-ddTHH:mm:ss') }"],
    capture_output=True, text=True).stdout
starts = sorted(line.strip() for line in out.splitlines() if line.strip())
written = dt.datetime.fromtimestamp(ingest_py.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
print(f"  ingest.py written : {written.replace('T', ' ')}")
if not starts:
    print("  celery started    : NO CELERY PROCESS RUNNING -- nothing is fetching")
else:
    print(f"  celery started    : {starts[0].replace('T', ' ')}")
    if starts[0] < written:
        print("  >> THE WORKER PREDATES THE CODE. It is still running the old gate.")
        print(r"     Fix: .\scripts\dev-stop.ps1 then .\scripts\dev-start.ps1")
    else:
        print("  >> Worker is newer than the code, so the fix is loaded.")

# -- 2. Redesign, or a page the fetcher never got? --------------------
rule("2. 'NO ELEMENTS MATCHED room_card' -- REDESIGN, OR NO PAGE?")

with sync_session() as session:
    rows = session.execute(text("""
        select e.occurred_at, h.name, e.context->>'selector' as selector,
               (e.context->>'page_text_chars')::int as chars, e.html_path
        from monitoring_errors e
        left join hotels h on h.id = e.hotel_id
        where e.message like 'No elements matched room_card%'
        order by e.occurred_at desc
        limit 20
    """)).all()

    if not rows:
        print("  No errors of this shape in the database.")

    for when, name, selector, chars, html in rows:
        print()
        print(f"  {when:%Y-%m-%d %H:%M}  {name}")
        print(f"    text the fetch could read : {chars} characters"
              f"   (a live hotel page renders 10,000-25,000)")

        saved = _find_saved_page(html)
        if saved is None:
            print("    saved page                : pruned (ARTIFACT_RETENTION_DAYS)")
            continue

        body = saved.read_text(encoding="utf-8", errors="replace")
        print(f"    saved page                : {saved.name}, {len(body):,} bytes")

        # PHRASES, never a bare "503". A three-digit number appears in any
        # megabyte of markup -- an element id, a price, a tracking token --
        # and matching on it reported the fully-rendered ASG page, all 1.9 MB
        # of it, as a block. The evidence has to be readable as English.
        blocks = [m for m in ("503 Service Unavailable", "502 Bad Gateway",
                              "Service Unavailable", "Access Denied",
                              "403 Forbidden", "captcha", "are you a robot",
                              "unusual traffic", "no server is available")
                  if m.lower() in body.lower()]
        if blocks:
            print(f"    >> BLOCK PAGE. The site refused us: {blocks[0]}")
            print("       The fetcher never had the hotel page, so nothing here "
                  "says anything about the selector.")
            continue

        # THE DECISIVE TEST. Every class in the selector it says matched
        # nothing, looked for in the page saved at that same moment. All
        # present means the markup was there and the fetch simply did not
        # have it yet -- the one thing "almost certainly a redesign" cannot
        # be true of.
        classes = re.findall(r"\.([A-Za-z0-9_-]+)", selector or "")
        if classes:
            found = {c: body.count(c) for c in classes}
            missing = [c for c, n in found.items() if n == 0]
            shown = "  ".join(f"{c}x{n}" for c, n in found.items())
            print(f"    selector in saved page    : {shown}")
            if not missing:
                print("    >> NOT A REDESIGN. Every class it claims is gone is in "
                      "the page it saved.")
            else:
                print(f"    >> genuinely absent: {', '.join(missing)} -- this one may "
                      "really be a redesign.")

    print()
    print("  A redesign fails EVERY check. Success rates over 10 days:")
    for name, status, n in session.execute(text("""
        select h.name, cr.status::text, count(*)
        from check_runs cr
        join monitor_targets mt on mt.id = cr.monitor_target_id
        join hotel_sources hs on hs.id = mt.hotel_source_id
        join hotels h on h.id = hs.hotel_id
        where cr.started_at > now() - interval '10 days'
        group by 1, 2 order by 1, 2
    """)).all():
        print(f"    {name[:34]:<34} {status:<9} {n}")
