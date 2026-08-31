"""Turning confirmed changes into the message a person reads.

Pure functions over :class:`ChangeLine`, so every message format can be tested
without a database, a template loader, or a mail server.

Two rules shape everything here:

* **A sold-out room is never a price of zero.** It gets its own sentence, its
  own icon, and no percentage. The whole comparison engine exists to keep that
  distinction intact; throwing it away in the last five lines of the pipeline
  would be an odd way to finish.
* **The message says what changed and by how much, in that order.** Someone
  reading this on a phone at 5:30 PM needs the hotel, the room, and the new
  price before anything else.
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.notifications.base import ChangeLine, RenderedMessage

_IST = "Asia/Kolkata"

_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def money(amount: Decimal | None, currency: str = "INR") -> str:
    """Indian digit grouping: ₹1,23,456, not ₹123,456.

    Worth the twelve lines. A price written in the wrong grouping reads as
    wrong to the person it is written for, and that erodes trust in the whole
    alert faster than a missed change would.

    Paise are shown only when they are non-zero. Booking engines genuinely
    quote half rupees — ₹1,202.50 is a real rate — and rounding it away made
    the dashboard disagree with the hotel's own page. Worse, Decimal's default
    banker's rounding turned 1202.50 into 1,202 and 2177.50 into 2,178, so the
    same .50 appeared to round in two directions.
    """
    if amount is None:
        return "—"
    symbol = _SYMBOLS.get(currency.upper(), f"{currency.upper()} ")

    value = Decimal(amount)
    fraction = abs(value) % 1
    if fraction:
        # Quantize to paise explicitly, half-up, so 0.005 never drifts.
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        whole = int(abs(value))
        paise = f"{abs(value) - whole:.2f}"[1:]   # ".50"
    else:
        value = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        whole = int(abs(value))
        paise = ""

    negative = value < 0
    digits = str(whole)

    if currency.upper() == "INR" and len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])
    else:
        grouped = f"{whole:,}"

    return f"{'-' if negative else ''}{symbol}{grouped}{paise}"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{abs(value):.1f}%"


def _headline(line: ChangeLine) -> str:
    if line.direction == "became_unavailable":
        return f"🚫 {line.room_name} — sold out"
    if line.direction == "became_available":
        return f"✅ {line.room_name} — available again at {money(line.new_price, line.currency)}"
    arrow = "▲" if line.direction == "increase" else "▼"
    word = "Increase" if line.direction == "increase" else "Decrease"
    basis = " vs last night" if line.is_overnight else ""
    return (
        f"{arrow} {line.room_name}: {money(line.old_price, line.currency)} → "
        f"{money(line.new_price, line.currency)}{basis}  "
        f"({word} {money(abs(line.delta) if line.delta else None, line.currency)}, "
        f"{_pct(line.delta_pct)})"
    )


def _stay(line: ChangeLine) -> str:
    return f"{_pretty_date(line.check_in)} → {_pretty_date(line.check_out)}"


def _pretty_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso


def checked_at_ist(when: datetime | None = None) -> str:
    when = when or datetime.now(ZoneInfo(_IST))
    return when.astimezone(ZoneInfo(_IST)).strftime("%-I:%M %p IST") if _supports_dash() \
        else when.astimezone(ZoneInfo(_IST)).strftime("%I:%M %p IST").lstrip("0")


def _supports_dash() -> bool:
    """``%-I`` is glibc-only; Windows strftime rejects it.

    Development happens on Windows and production runs on Linux, so this is
    checked rather than assumed — a crash in the notification renderer would
    silently drop alerts on one platform only.
    """
    try:
        datetime.now().strftime("%-I")
        return True
    except ValueError:
        return False


def render_digest(
    hotel_name: str, lines: list[ChangeLine], *, when: datetime | None = None
) -> RenderedMessage:
    """One message covering every change for one hotel in this window.

    Batching is not a nicety. A weekend-wide reprice produces a hundred
    changes in one cycle, and a hundred separate WhatsApps at 5:30 PM gets the
    system muted permanently — after which no alert reaches anyone at all.
    """
    stamp = checked_at_ist(when)
    count = len(lines)

    subject = (
        f"Price change: {hotel_name} — {_headline_summary(lines)}"
        if count == 1
        else f"{count} price changes: {hotel_name}"
    )

    body_lines = [
        "🔔 Room Price Changed" if count == 1 else f"🔔 {count} Room Price Changes",
        f"Hotel: {hotel_name}",
        "",
    ]
    for line in lines:
        body_lines.append(_headline(line))
        body_lines.append(f"   Stay: {_stay(line)}"
                          + (f"   Plan: {line.meal_plan}" if line.meal_plan else ""))
        body_lines.append("")
    body_lines.append(f"Checked: {stamp}")

    return RenderedMessage(
        subject=subject,
        text="\n".join(body_lines),
        html=_render_html(hotel_name, lines, stamp),
        template_params=_whatsapp_params(hotel_name, lines, stamp),
    )


def _headline_summary(lines: list[ChangeLine]) -> str:
    line = lines[0]
    if line.direction == "became_unavailable":
        return f"{line.room_name} sold out"
    return f"{line.room_name} {money(line.new_price, line.currency)}"


def _render_html(hotel_name: str, lines: list[ChangeLine], stamp: str) -> str:
    """Deliberately table-based and inline-styled.

    Email clients — Outlook above all — do not support the CSS that would make
    this pleasant to write. A layout that renders correctly everywhere is
    worth more than clean markup nobody sees.
    """
    rows = []
    for line in lines:
        if line.direction == "became_unavailable":
            change_cell = '<span style="color:#b45309;font-weight:600">Sold out</span>'
            new_cell = "—"
        elif line.direction == "became_available":
            change_cell = '<span style="color:#047857;font-weight:600">Available again</span>'
            new_cell = money(line.new_price, line.currency)
        else:
            colour = "#b91c1c" if line.direction == "increase" else "#047857"
            sign = "+" if line.direction == "increase" else "−"
            change_cell = (
                f'<span style="color:{colour};font-weight:600">{sign}'
                f"{money(abs(line.delta) if line.delta else None, line.currency)}"
                f" ({_pct(line.delta_pct)})</span>"
            )
            new_cell = money(line.new_price, line.currency)

        rows.append(
            "<tr>"
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{_esc(line.room_name)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280">'
            f"{money(line.old_price, line.currency)}</td>"
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">{new_cell}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{change_cell}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;'
            f'white-space:nowrap">{_esc(_stay(line))}</td>'
            "</tr>"
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f9fafb;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
  <table role="presentation" width="100%" style="max-width:640px;margin:0 auto;
    background:#ffffff;border:1px solid #e5e7eb;border-radius:8px" cellpadding="0" cellspacing="0">
    <tr><td style="padding:20px 24px;border-bottom:1px solid #e5e7eb">
      <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280">
        Price change</div>
      <div style="font-size:20px;font-weight:700;margin-top:4px">{_esc(hotel_name)}</div>
    </td></tr>
    <tr><td style="padding:8px 12px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="border-collapse:collapse;font-size:14px">
        <tr style="text-align:left;color:#6b7280;font-size:12px">
          <th style="padding:8px 12px">Room</th><th style="padding:8px 12px">Was</th>
          <th style="padding:8px 12px">Now</th><th style="padding:8px 12px">Change</th>
          <th style="padding:8px 12px">Stay</th>
        </tr>
        {"".join(rows)}
      </table>
    </td></tr>
    <tr><td style="padding:16px 24px;color:#6b7280;font-size:12px;border-top:1px solid #e5e7eb">
      Checked {_esc(stamp)} · Hotel Price Monitor
    </td></tr>
  </table>
</body></html>"""


def _whatsapp_params(hotel_name: str, lines: list[ChangeLine], stamp: str) -> list[str]:
    """Positional variables for the approved WhatsApp template.

    Order is fixed by the template Meta approved, so this list is a contract:
    ``{{1}} hotel  {{2}} room  {{3}} old  {{4}} new  {{5}} delta  {{6}} dates
    {{7}} time``. When several changes are batched, the first is shown in full
    and the rest are summarised, because a template's variables cannot expand
    into a table.

    The length is a contract with the provider, which refuses to send any other
    count: see ``base.WHATSAPP_TEMPLATE_PARAM_COUNT``.
    """
    line = lines[0]
    room = line.room_name if len(lines) == 1 else f"{line.room_name} +{len(lines) - 1} more"
    if line.direction == "became_unavailable":
        delta = "sold out"
    elif line.delta is not None:
        sign = "+" if line.direction == "increase" else "-"
        delta = f"{sign}{money(abs(line.delta), line.currency)} ({_pct(line.delta_pct)})"
    else:
        delta = "now available"

    return [
        hotel_name,
        room,
        money(line.old_price, line.currency),
        money(line.new_price, line.currency),
        delta,
        _stay(line),
        stamp,
    ]


def _esc(text: str) -> str:
    """Escape for HTML email.

    Room names come from someone else's website, which makes them untrusted
    input no matter how ordinary they look.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
