"""Record a captioned walkthrough of the dashboard as a video.

    python scripts/record_demo.py                      # local, as the first active admin
    python scripts/record_demo.py --user ags@123 --out demo

Drives a real browser through the dashboard with Playwright, lays a caption
banner over each screen and saves what it saw as ``<out>/hotel-price-monitor-demo.webm``.

It is READ-ONLY by construction: it navigates, scrolls and hovers, and never
presses a button. Repricing's "Apply to RMS", a hotel's "Run now" and every
Save are on screen but untouched, so this is safe to run against production.

No password is asked for. It signs a session for an existing, active user with
this deployment's own SECRET_KEY, the same way a login does — which is also why
it has to run on the machine whose .env serves the dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import Page, sync_playwright  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.api.deps import SESSION_COOKIE  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import get_async_session_factory  # noqa: E402

WIDTH, HEIGHT = 1440, 810

# The overlay lives in the page, so it is re-injected after every navigation.
OVERLAY_JS = """
() => {
  if (document.getElementById('demo-cap')) return;
  const css = document.createElement('style');
  css.textContent = `
    #demo-cap { position: fixed; left: 50%; bottom: 28px; transform: translate(-50%, 20px);
      width: min(1100px, 90vw); background: rgba(10, 40, 36, .94); color: #fff;
      border-radius: 14px; padding: 18px 26px 20px; z-index: 2147483646;
      font: 500 22px/1.4 system-ui, 'Segoe UI', sans-serif; opacity: 0;
      box-shadow: 0 12px 40px rgba(0,0,0,.35); transition: opacity .45s, transform .45s; }
    #demo-cap.on { opacity: 1; transform: translate(-50%, 0); }
    #demo-cap .tag { display: inline-block; font-size: 13px; font-weight: 700;
      letter-spacing: .12em; text-transform: uppercase; color: #7fe0cc; margin-bottom: 6px; }
    #demo-ring { position: absolute; border: 4px solid #ff4d6d; border-radius: 12px;
      box-shadow: 0 0 0 9999px rgba(0,0,0,.18); z-index: 2147483645; pointer-events: none;
      transition: all .5s; opacity: 0; }
    #demo-ring.on { opacity: 1; }
    #demo-cursor { position: fixed; width: 22px; height: 22px; border-radius: 50%;
      background: rgba(255, 77, 109, .55); border: 2px solid #fff; z-index: 2147483647;
      pointer-events: none; transform: translate(-50%, -50%); left: -50px; top: -50px; }`;
  document.head.appendChild(css);
  const cap = document.createElement('div'); cap.id = 'demo-cap';
  const ring = document.createElement('div'); ring.id = 'demo-ring';
  const cur = document.createElement('div'); cur.id = 'demo-cursor';
  document.body.append(cap, ring, cur);
  document.addEventListener('mousemove', e => {
    cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; });
}
"""


class Director:
    """Small vocabulary for a scene: open, say, ring, scroll, glide, wait."""

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base = base_url.rstrip("/")
        self.mouse = (WIDTH // 2, HEIGHT // 2)

    def open(self, path: str) -> None:
        self.page.goto(self.base + path, wait_until="networkidle", timeout=90_000)
        self.page.evaluate(OVERLAY_JS)
        self.page.mouse.move(*self.mouse)
        self.wait(0.8)

    def say(self, tag: str, text: str, seconds: float = 6) -> None:
        self.page.evaluate(
            """([tag, text]) => { const c = document.getElementById('demo-cap');
                 c.classList.remove('on');
                 setTimeout(() => { c.innerHTML = '<div class="tag"></div><div class="t"></div>';
                   c.querySelector('.tag').textContent = tag;
                   c.querySelector('.t').textContent = text; c.classList.add('on'); }, 250); }""",
            [tag, text],
        )
        self.wait(seconds)

    def hide(self) -> None:
        self.page.evaluate("() => document.getElementById('demo-cap').classList.remove('on')")
        self.wait(0.5)

    def ring(self, selector: str | None, seconds: float = 0) -> bool:
        """Outline the first match; ``None`` clears. Missing elements are skipped, not fatal."""
        found = self.page.evaluate(
            """(sel) => { const r = document.getElementById('demo-ring');
                 if (!sel) { r.classList.remove('on'); return true; }
                 const el = document.querySelector(sel); if (!el) { r.classList.remove('on'); return false; }
                 const b = el.getBoundingClientRect(), pad = 8;
                 Object.assign(r.style, { left: (b.left + scrollX - pad) + 'px', top: (b.top + scrollY - pad) + 'px',
                   width: (b.width + 2 * pad) + 'px', height: (b.height + 2 * pad) + 'px' });
                 r.classList.add('on'); return true; }""",
            selector,
        )
        self.wait(seconds)
        return found

    def scroll_to(self, selector: str, offset: int = 120) -> None:
        target = self.page.locator(selector).first
        if target.count():
            target.evaluate(
                """(el, off) => window.scrollTo({ top: el.getBoundingClientRect().top + scrollY - off,
                                                  behavior: 'smooth' })""",
                offset,
            )
        self.wait(1.4)

    def scroll_by(self, dy: int, seconds: float = 1.4) -> None:
        self.page.evaluate("(dy) => window.scrollBy({ top: dy, behavior: 'smooth' })", dy)
        self.wait(seconds)

    def top(self) -> None:
        self.page.evaluate("() => window.scrollTo({ top: 0, behavior: 'smooth' })")
        self.wait(1.2)

    def glide(self, selector: str) -> None:
        """Move the visible cursor onto an element — hover only, never a click."""
        box = self.page.locator(selector).first.bounding_box() if self.page.locator(selector).count() else None
        if not box:
            return
        target = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        self.page.mouse.move(*target, steps=25)
        self.mouse = target
        self.wait(0.6)

    def card(self, title: str, subtitle: str, seconds: float) -> None:
        self.page.set_content(f"""<html><body style="margin:0;height:100vh;display:flex;align-items:center;
            justify-content:center;background:linear-gradient(135deg,#0b3d36,#15806f);
            font-family:system-ui,'Segoe UI',sans-serif;color:#fff;text-align:center">
            <div><div style="font-size:64px;font-weight:800;letter-spacing:-.01em">{title}</div>
            <div style="font-size:28px;opacity:.85;margin-top:18px;max-width:1000px">{subtitle}</div></div>
            </body></html>""")
        self.wait(seconds)

    def wait(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))


def walkthrough(d: Director) -> None:
    d.card("Hotel Price Monitor",
           "Every competitor's room price, checked every 30 minutes — and your own rates kept in step.", 7)

    # 1. Overview
    d.open("/")
    d.say("Overview", "Log in and the overview tells you, at a glance, how your market looks right now.", 6)
    d.ring("section.tiles", 0)
    d.say("Overview", "How many prices changed in the last 24 hours, how many rooms were priced today, "
                      "and when the last successful check ran.", 7)
    d.ring(None)
    d.say("Overview", "Only real moves count: a change must clear ₹50 and 2%, and still be there on the "
                      "next check — so you are never alerted for a one-off blip.", 8)
    d.scroll_by(420, 1.5)
    d.say("Overview", "Every hotel you watch has a card showing whether its prices are current.", 6)

    # 2. Matrix
    d.open("/matrix")
    d.say("Price matrix", "The price matrix: every competitor, every room type, tonight's price — side by side.", 7)
    d.ring("tr:has(.pill-own)", 0)
    d.say("Price matrix", "Your own property is highlighted, so you always see where you stand.", 6)
    d.ring(None)
    d.glide("a.chip[href*='category=']")
    d.say("Price matrix", "Filter by room category — Classic, Deluxe, Suite — to compare like with like.", 6)
    d.scroll_by(520, 1.6)
    d.say("Price matrix", "Sold-out rooms are marked as sold out, with the last price seen before they filled up.", 7)
    d.scroll_by(520, 1.6)
    d.say("Price matrix", "The check-in date, check-out date and number of guests at the top let you look at any night.", 6)

    # 3. Comparison
    d.open("/comparison")
    d.say("Comparison", "Comparison puts each competitor's cheapest room in every category against yours.", 7)
    d.scroll_by(330, 1.5)
    d.say("Comparison", "Red means they are cheaper than you and undercutting your rate. "
                        "Green means they are dearer. The exact gap is shown in rupees and as a percentage.", 9)
    d.scroll_by(300, 1.5)
    d.say("Comparison", "Which competitor is underpricing you, and by how much, without checking a single booking site.", 7)

    # 4. Changes
    d.open("/changes?hours=720")
    d.say("Changes", "Every confirmed price change is recorded here: the hotel, the room, the stay, "
                     "the old price and the new one.", 8)
    d.scroll_by(350, 1.5)
    d.say("Changes", "Filter by hotel or date range to see how a competitor has priced over the last month.", 7)

    # 5. History
    d.open("/price-history")
    d.say("Price history", "Price history keeps every reading of every room, check after check.", 7)
    d.glide("a[href*='/price-history/export']")
    d.say("Price history", "Export it to Excel for your own analysis in one click.", 6)
    d.scroll_by(700, 2)
    d.say("Price history", "Weekends, festivals, long weekends: you can see exactly how the market reacts.", 7)

    # 6. Hotels
    d.open("/hotels")
    d.say("Hotels", "Adding a competitor takes minutes. Give it the hotel's booking page and the system finds the prices itself.", 8)
    hrefs = d.page.locator("main a[href^='/hotels/']").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    href = next((h for h in hrefs if h.rstrip("/").split("/")[-1].isdigit()), None)
    if href:
        d.open(href)
        d.say("Hotels", "Each hotel shows what is being watched, how often, and whether its data is healthy.", 7)
        d.say("Hotels", "If a booking site changes its layout, the monitor repairs its own price reader "
                        "automatically — no one has to fix it by hand.", 8)

    # 7. Alerts
    d.open("/notifications")
    d.say("Alerts", "When a competitor's price really changes, the person responsible gets a WhatsApp alert within minutes.", 8)
    d.say("Alerts", "Changes for the same hotel are grouped into one message, so a big repricing never floods your phone.", 7)
    d.scroll_to("h2:has-text('What the repricer did')", 140)
    d.say("Alerts", "Below it, a log of every rate the system changed for you — what the market was, "
                    "what it aimed for, and what it set.", 8)

    # 8. Repricing
    d.open("/repricing")
    d.say("Repricing", "Repricing turns market prices into your rates. For each of your rooms it proposes tonight's price.", 8)
    d.scroll_to("button:has-text('Preview in RMS')", 260)
    d.glide("button:has-text('Preview in RMS')")
    d.say("Repricing", "Preview shows exactly what would change, without changing anything.", 6)
    d.glide("button:has-text('Apply to RMS now')")
    d.say("Repricing", "Apply writes the new rates straight into your rate manager, so you never type them in by hand.", 7)
    d.ring("[data-endpoint$='repricing/automatic']", 0)
    d.say("Repricing", "Or turn on Automatic and the system keeps your rates in step with the market, day and night.", 7)
    d.ring(None)
    d.scroll_to("h2:has-text('Room mapping')", 120)
    d.say("Repricing", "You stay in control: every room has a floor and a ceiling the system will never cross.", 8)

    # 9. Attention
    d.open("/attention")
    d.say("Attention", "If a source stops answering or a new room type appears, it shows up here, "
                       "so a quiet day is never mistaken for a broken check.", 8)

    d.card("Never be the last to know.",
           "Competitor prices every 30 minutes · WhatsApp alerts · Your rates kept in step automatically", 8)


async def _session_token(username: str | None) -> str:
    async with get_async_session_factory()() as session:
        query = select(User).where(User.is_active.is_(True))
        query = query.where(User.username == username) if username else query.order_by(User.id)
        user = (await session.scalars(query)).first()
    if user is None:
        raise SystemExit(f"No active user {username or ''} in this database.".replace("  ", " "))
    print(f"recording as: {user.username}")
    return create_access_token(str(user.id), user.role.value, must_change_password=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--user", help="username to record as (default: the first active user)")
    ap.add_argument("--out", default="demo", help="directory for the video (default: ./demo)")
    ap.add_argument("--headed", action="store_true", help="show the browser while recording")
    args = ap.parse_args()

    token = asyncio.run(_session_token(args.user))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(out / "raw"),
            record_video_size={"width": WIDTH, "height": HEIGHT},
        )
        context.add_cookies([{"name": SESSION_COOKIE, "value": token, "url": args.base_url}])
        page = context.new_page()
        walkthrough(Director(page, args.base_url))
        video = page.video
        context.close()  # the video is only finalised when its context closes
        browser.close()
        final = out / "hotel-price-monitor-demo.webm"
        shutil.move(video.path(), final)
        shutil.rmtree(out / "raw", ignore_errors=True)
    print(f"saved: {final.resolve()}")


if __name__ == "__main__":
    main()
