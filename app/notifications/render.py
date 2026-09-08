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

TWO MESSAGES, TWO TRIGGERS
==========================
:func:`render_digest` answers "what just moved?" -- one hotel, the rooms that
changed, the deltas. It is sent the moment a change is confirmed.

:func:`render_summary` answers "what moved this window?" -- how many rooms
changed price in the last N hours, which ones, and by how much. It is sent on
a clock, every ``summary_interval_hours``, and never in response to a single
change.

Neither replaces the other and both go out. A delta is the right message when
one room moves at 2 AM; a counted window is the right message when somebody
sits down at 4 PM and wants to know whether the afternoon was busy. They are
different jobs, not different amounts of detail.

NEITHER OF THEM CARRIES THE COMPARISON GRID
===========================================
Where a rate sits against the market is a third question, asked at a different
moment, and /comparison answers it on a screen with room for a table. Printing
every room of every property beside four moves buries the four.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.notifications.base import (
    MARKET_COMPARISON,
    WHATSAPP_COMPARISON_MIN_PARAMS,
    ChangeLine,
    RenderedMessage,
)

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
    # NO "vs last night" HERE, though ``line.is_overnight`` still says which
    # moves are one. It was four words on a line the reader scans for two
    # numbers, and on WhatsApp -- where every move is one clause in a run-on
    # paragraph -- repeating it made the whole variable harder to read than
    # the distinction was worth.
    #
    # The distinction itself is not gone: /changes and the overview carry it
    # as a pill, on screens with room to explain it in a tooltip. This is the
    # message deciding it has less room than a page does, not the system
    # deciding a rolled-forward night is the same as a reprice.
    return (
        f"{arrow} {line.room_name}: {money(line.old_price, line.currency)} → "
        f"{money(line.new_price, line.currency)}  "
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
    # BOTH availability directions are decided BEFORE the delta is consulted,
    # the way ``_headline`` and ``_render_html`` already decide them.
    #
    # Testing ``delta is not None`` first read the wrong field on a room coming
    # back: a became_available row carries delta 0.00 -- a real number, not a
    # NULL, because the price it returned at is compared against the price it
    # left at. So it fell into the price-move branch, took the "not an
    # increase" sign, and a room returning to sale was announced as
    #
    #     Change: -₹0 (0.0%)
    #
    # while "now available" sat in an else that nothing could reach. The email
    # for the same change said "available again" correctly, which is why this
    # survived: the two channels disagreed and only the quiet one was wrong.
    if line.direction == "became_unavailable":
        delta = "sold out"
    elif line.direction == "became_available":
        delta = "now available"
    elif line.delta is not None:
        sign = "+" if line.direction == "increase" else "-"
        delta = f"{sign}{money(abs(line.delta), line.currency)} ({_pct(line.delta_pct)})"
    else:
        # A priced direction with no delta is a data fault, not a free room.
        delta = "—"

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


# ── the market summary message ───────────────────────────────────────
#: Longest one WhatsApp template variable may be before the providers cut it.
#:
#: Both cap at 700 characters and end a too-long value with an ellipsis. That
#: is the right behaviour for a scraped room name and the wrong one for a list
#: of price moves: the reader cannot tell a window that ended at four rooms
#: from one that was cut off there. So the renderer works to a smaller figure
#: and does its own trimming, at a property boundary, saying how many it left
#: out.
_WHATSAPP_PARAM_BUDGET = 620

#: Separates one property from the next inside a single template variable.
#: Not a comma: the My Dreams reseller splits parameters on commas, so a comma
#: here would shift every later variable into the wrong slot.
_SEGMENT = " • "

#: How many of the template's variables are NOT moved rooms: the count with
#: its window, and the time. Everything between them carries the moves.
_FIXED_PARAMS = 2

#: What goes in a slot the window did not fill.
#:
#: Never an empty string -- Meta rejects an empty variable as 132005, which is
#: permanent, so a quiet window would become an undeliverable alert.
#:
#: A dash rather than a sentence. The template puts a label in front of every
#: slot, so the line already reads "Continued: ..." and a full sentence after
#: it says the same thing twice, three times over on a quiet window. On a phone
#: those repeats are longer than some of the moves above them.
_UNUSED_SLOT = "—"


def render_summary(
    moved: Sequence[ChangeLine],
    *,
    window_hours: int = 0,
    when: datetime | None = None,
    param_count: int = WHATSAPP_COMPARISON_MIN_PARAMS,
) -> RenderedMessage:
    """How many rooms moved in the window, which ones, and by how much.

    Two things, in the order somebody reads them: the count, then the moves.

    The count leads because it is the question the message answers. "Four rooms
    moved in the last two hours" is a decision on its own -- on a quiet evening
    it says the market is asleep and nothing needs doing, and that is worth
    knowing before a single figure is read.

    WHAT THIS DELIBERATELY DOES NOT CARRY
    =====================================
    The comparison grid. A message about a window is about what CHANGED in it,
    and printing every room of every property alongside four moves buries the
    four -- the reader has to find them in a table that is mostly rows saying
    nothing happened. Where a rate sits against the market is a different
    question, it is asked at a different moment, and /comparison answers it on
    a screen with room for a table.

    ``param_count`` is how many body variables the approved WhatsApp template
    has, which the caller reads from configuration. It affects the WhatsApp
    parameters ONLY -- the text and the HTML always carry every move, because
    neither channel has a variable to overflow.

    Nothing here touches a database or a clock except through ``when``.
    """
    stamp = checked_at_ist(when)
    headline = _moved_headline(len(moved), window_hours)

    return RenderedMessage(
        subject=headline,
        text=_summary_text(headline, moved, stamp),
        html=_summary_html(headline, moved, stamp),
        template_params=_summary_params(headline, moved, stamp, param_count),
        kind=MARKET_COMPARISON,
    )


def _moved_headline(count: int, window_hours: int) -> str:
    """"4 rooms changed price in the last 2 hours" — the message in one line.

    Both halves are pluralised rather than written "room(s)": this is the
    subject line and the first WhatsApp variable, the two places a reader
    decides whether to open anything, and a message that cannot conjugate is a
    message that looks automated enough to ignore.

    The window is the one actually covered, not the configured interval. A
    worker that was down for six hours sends a six-hour window on its next
    tick, and saying "the last 2 hours" over six hours of movement would be a
    wrong number stated confidently -- which is the failure this whole system
    exists to prevent.
    """
    rooms = "room" if count == 1 else "rooms"
    if window_hours <= 0:
        return f"{count} {rooms} changed price"
    hours = "hour" if window_hours == 1 else "hours"
    return f"{count} {rooms} changed price in the last {window_hours} {hours}"


def _by_hotel(moved: Sequence[ChangeLine]) -> dict[str, list[ChangeLine]]:
    """The moves grouped under the property each belongs to.

    Grouped because a window can span four properties, and a flat list repeats
    the property name on every line -- the wrapping that made the per-hotel
    digest unreadable on a phone before it grouped too.

    Names are whitespace-normalised on the way in. They are scraped, and this
    deployment has one stored as "TREEBO MIDVALLEY  RESIDENCY " -- a double
    space and a trailing one, which rendered as "RESIDENCY :" with the colon
    adrift. Normalising here rather than at ingest because the stored name is
    the hotel's own and correcting it is a different decision; what a message
    must not do is make the system look careless about the numbers beside it.

    It also merges two spellings that differ only in spacing, which would
    otherwise print the same property twice under two headings.
    """
    out: dict[str, list[ChangeLine]] = {}
    for line in moved:
        out.setdefault(" ".join(line.hotel_name.split()), []).append(line)
    return out


def _summary_text(headline: str, moved: Sequence[ChangeLine], stamp: str) -> str:
    """The plain-text body: the count, then a block per property."""
    lines = [f"📊 {headline}", ""]

    for hotel, rooms in _by_hotel(moved).items():
        lines.append(hotel)
        for line in rooms:
            lines.append(f"  {_headline(line)}")
            lines.append(f"      {_stay(line)}")
        lines.append("")

    if not moved:
        # Defensive. A window with nothing in it never produces a message at
        # all, so reaching here means the moves were lost between the query and
        # the render -- which must say so rather than print a bare count.
        lines.append("(no change was recorded in this window)")
        lines.append("")

    lines.append(f"Checked: {stamp}")
    return "\n".join(lines)


def _summary_params(
    headline: str,
    moved: Sequence[ChangeLine],
    stamp: str,
    param_count: int = WHATSAPP_COMPARISON_MIN_PARAMS,
) -> list[str]:
    """Positional variables for the market-summary WhatsApp template.

    Two are fixed and everything between them carries the moves, so a
    five-variable template lays out as::

        {{1}} how many moved, over what window
        {{2}} {{3}} {{4}} the rooms that moved
        {{5}} time

    and a seven-variable one puts moves in {{2}} through {{6}}. The order is a
    contract with whatever Meta approved, which is why the fixed two sit at the
    ends: a bigger template is the same message with more room in it, not a
    different message.

    A template variable cannot contain a newline -- Meta rejects it as 132005 --
    so the moves arrive as run-on text. Properties are separated by a bullet
    and rooms by a semicolon, which survives both transports; a comma would not
    (see the My Dreams provider).
    """
    slots = max(1, param_count - _FIXED_PARAMS)

    # An en dash, not a colon. The template labels each slot -- "Property: ..."
    # -- so a colon here made every line read "Property: STERLING: ▼ Classic
    # Room: ..." with three of them before the first number.
    segments = [
        f"{hotel} – " + "; ".join(_headline(line) for line in rooms)
        for hotel, rooms in _by_hotel(moved).items()
    ]
    packed, dropped = _fit(segments, slots, _WHATSAPP_PARAM_BUDGET)

    if dropped:
        # Said in the count rather than appended to a slot, so the sentence the
        # reader trusts most is the one that admits what is missing.
        headline += f" ({dropped} more on the dashboard)"

    return [headline, *packed, stamp]


def _fit(segments: list[str], slots: int, budget: int) -> tuple[list[str], int]:
    """Lay properties out across ``slots`` variables, and say how many did not.

    ONE PROPERTY PER SLOT WHILE THERE ARE SLOTS TO SPARE
    ===================================================
    A template variable cannot hold a newline, so everything packed into one
    arrives as a single run-on paragraph. Packed greedily to the budget, three
    properties became one wall of text under "Rooms and properties:" while the
    three slots below it said "no further changes" -- the worst of both: an
    unreadable line AND three wasted ones. The template already puts each slot
    on its own line, so spreading them is free and gives the reader the line
    breaks the variable cannot contain.

    Only when there are more properties than slots does it pack, and then it
    packs to the budget as before -- because at that point the choice is
    between a dense line and losing a property, and a dense line wins.

    Trimmed at a property boundary and never mid-figure. A rate cut in half by
    a character count is a wrong number presented as a right one, which is the
    one thing these messages may not do -- and it is exactly what the providers'
    own 700-character ellipsis would do if this did not run first.

    A property longer than the whole budget is still kept rather than skipped:
    the provider will shorten that one, and a variable reading "…" is better
    than an empty one, which Meta rejects outright as 132005. Keeping it also
    guarantees progress, so a single enormous room name cannot stall the loop.

    Slots nothing was left for carry ``_UNUSED_SLOT``, for the same reason:
    every variable the template declares must arrive, and must not be empty.
    """
    if not segments:
        return ["no room changed price in this window",
                *([_UNUSED_SLOT] * (slots - 1))], 0

    if len(segments) <= slots:
        return [*segments, *([_UNUSED_SLOT] * (slots - len(segments)))], 0

    filled: list[str] = []
    index = 0
    while len(filled) < slots and index < len(segments):
        kept: list[str] = []
        used = 0
        while index < len(segments):
            segment = segments[index]
            cost = len(segment) + (len(_SEGMENT) if kept else 0)
            if kept and used + cost > budget:
                break
            kept.append(segment)
            used += cost
            index += 1
        filled.append(_SEGMENT.join(kept))

    dropped = len(segments) - index
    filled.extend([_UNUSED_SLOT] * (slots - len(filled)))
    return filled, dropped


def _summary_html(headline: str, moved: Sequence[ChangeLine], stamp: str) -> str:
    """The count, then every move, grouped by property.

    Email carries all of them -- the WhatsApp slots have a hard ceiling and
    email has none, and a reader who opens the email is the one who wanted the
    detail the message could not carry.

    Table-based and inline-styled for the same reason ``_render_html`` is:
    Outlook.
    """
    blocks = []
    for hotel, rooms in _by_hotel(moved).items():
        items = "".join(
            f'<li style="margin:8px 0">{_esc(_headline(line))}'
            f'<div style="color:#6b7280;font-size:12px">{_esc(_stay(line))}</div></li>'
            for line in rooms
        )
        blocks.append(
            f'<div style="margin:16px 0 0"><div style="font-weight:600">{_esc(hotel)}</div>'
            f'<ul style="margin:6px 0 0;padding-left:18px;font-size:14px">{items}</ul></div>'
        )

    body = "".join(blocks) or (
        '<p style="margin:0;color:#b45309;font-size:13px">No change was recorded '
        "in this window.</p>"
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f9fafb;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
  <table role="presentation" width="100%" style="max-width:640px;margin:0 auto;
    background:#ffffff;border:1px solid #e5e7eb;border-radius:8px" cellpadding="0" cellspacing="0">
    <tr><td style="padding:20px 24px;border-bottom:1px solid #e5e7eb">
      <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280">
        Market update</div>
      <div style="font-size:20px;font-weight:700;margin-top:4px">{_esc(headline)}</div>
    </td></tr>
    <tr><td style="padding:8px 24px 20px">{body}</td></tr>
    <tr><td style="padding:16px 24px;color:#6b7280;font-size:12px;border-top:1px solid #e5e7eb">
      Checked {_esc(stamp)} · Hotel Price Monitor
    </td></tr>
  </table>
</body></html>"""
