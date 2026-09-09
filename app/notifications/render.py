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


def _flat(text: str) -> str:
    """One line of whitespace-normalised text, for anything scraped.

    Room names come off other people's markup and arrive with newlines and
    runs of spaces in them. That used to be the transport's problem -- both
    providers flattened every parameter on the way out -- but the message now
    puts DELIBERATE newlines in a slot, so a provider can no longer tell a line
    break that means something from one that came out of a <br> in somebody's
    room table. It is decided here, where the difference is known.
    """
    return " ".join(str(text).split())


def _headline(line: ChangeLine) -> str:
    if line.direction == "became_unavailable":
        return f"🚫 {_flat(line.room_name)} — sold out"
    if line.direction == "became_available":
        return (
            f"✅ {_flat(line.room_name)} — available again at "
            f"{money(line.new_price, line.currency)}" + _marker(line)
        )
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
        f"{arrow} {_flat(line.room_name)}: {money(line.old_price, line.currency)} → "
        f"{money(line.new_price, line.currency)}  "
        f"({word} {money(abs(line.delta) if line.delta else None, line.currency)}, "
        f"{_pct(line.delta_pct)})"
    ) + _marker(line)


def _marker(line: ChangeLine) -> str:
    """The tax note for one line, where it disagrees with the rest of the message.

    Appended rather than given its own line: on a phone the note belongs to
    the figure beside it, and a message that puts qualifications on their own
    lines is twice as long for the same content.

    Empty for the ordinary line. The footer already states the basis the whole
    message is on, and repeating it against every room turns the one line that
    is genuinely different into more of the same.
    """
    return f"  {line.basis_note}" if line.basis_note else ""


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


#: What the message says about the basis its prices are on.
#:
#: SAID ONCE, IN THE PLACE EVERY CHANNEL ALREADY HAS
#: =================================================
#: Appended to the "checked at" stamp rather than given a line of its own,
#: because that stamp is the one string that already reaches all three
#: renderings -- the text body, the email footer, and the last variable of
#: both approved WhatsApp templates. A separate footer would need a new
#: template variable, and a new template needs Meta's approval; this needs
#: nothing and cannot drift between channels.
#:
#: SAID IN BOTH DIRECTIONS
#: =======================
#: Off is not "no claim", it is the claim that ₹9,000 is a room a guest pays
#: ₹10,620 for. That silence is what made "are we sending prices with tax?" a
#: question somebody had to read the source to answer. Rooms whose site
#: publishes only the other component still carry their own marker -- the
#: footer is the general case and the marker is the exception, the same way
#: the matrix labels a cell rather than a column.
_BASIS_NOTE = {True: "prices incl. tax", False: "prices excl. tax"}


def _stamp(when: datetime | None, with_tax: bool) -> str:
    return f"{checked_at_ist(when)} · {_BASIS_NOTE[bool(with_tax)]}"


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
    hotel_name: str,
    lines: list[ChangeLine],
    *,
    when: datetime | None = None,
    with_tax: bool = False,
) -> RenderedMessage:
    """One message covering every change for one hotel in this window.

    Batching is not a nicety. A weekend-wide reprice produces a hundred
    changes in one cycle, and a hundred separate WhatsApps at 5:30 PM gets the
    system muted permanently — after which no alert reaches anyone at all.

    ``with_tax`` does NOT choose the numbers. They arrive already chosen, on
    the lines, because choosing them needs the components off a database row
    and this module touches no database. It says which basis was chosen, so
    the message can state it -- and it must therefore agree with what the
    caller put in ``line.old_price`` and ``line.new_price``. See
    ``workers/tasks_notify._render_lines``, which does both in one place.
    """
    stamp = _stamp(when, with_tax)
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

        # The same marker the text body appends, in the cell it belongs to.
        # Muted and small: it qualifies the figure, it is not a second figure.
        if line.basis_note:
            new_cell += (
                f'<span style="color:#6b7280;font-weight:400;font-size:12px"> '
                f"{_esc(line.basis_note)}</span>"
            )

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

    # The marker goes on the "now" figure, which is the one a reader acts on,
    # and on the "was" figure when there is no "now" -- a sold-out room still
    # quotes a price and it still has a basis. Never on both: they sit next to
    # each other under two labels, and saying it twice in a template that
    # allows no line breaks costs more room than it buys.
    old_cell = money(line.old_price, line.currency)
    new_cell = money(line.new_price, line.currency)
    if line.basis_note:
        if line.new_price is not None:
            new_cell = f"{new_cell} {line.basis_note}"
        elif line.old_price is not None:
            old_cell = f"{old_cell} {line.basis_note}"

    return [
        hotel_name,
        room,
        old_cell,
        new_cell,
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
#:
#: A BLANK LINE, BECAUSE A VARIABLE CAN HOLD ONE
#: =============================================
#: Meta documents a newline inside a template parameter as rejected, and both
#: providers flattened every parameter on that basis for as long as this
#: message has existed. Measured on the live reseller path on 9 Sep 2026, it is
#: not true: a parameter carrying "\n" was accepted (a real wamid came back)
#: and arrived on the handset broken across the lines it asked for.
#:
#: That is the difference between a message whose layout is fixed by whatever
#: Meta approved and one that lays itself out. When two properties have to
#: share a slot they are still two blocks, separated the way the template
#: separates its own slots.
#:
#: Not a comma, whatever else changes: the My Dreams reseller splits parameters
#: on commas, so a comma here would shift every later variable into the wrong
#: slot.
_SEGMENT = "\n\n"

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
    with_tax: bool = False,
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
    stamp = _stamp(when, with_tax)
    headline = _moved_headline(moved, window_hours)

    return RenderedMessage(
        subject=headline,
        text=_summary_text(headline, moved, stamp),
        html=_summary_html(headline, moved, stamp),
        template_params=_summary_params(headline, moved, stamp, param_count),
        kind=MARKET_COMPARISON,
    )


def _moved_headline(moved: Sequence[ChangeLine], window_hours: int) -> str:
    """"4 rooms changed price in the last 2 hours" — the message in one line.

    COUNTS ROOMS, NOT PRICE CHANGES, AND THE TWO ARE NOT THE SAME
    ============================================================
    A room can reprice several times inside one window. This counted the moves
    and called them rooms: a window holding six changes across three rooms --
    one of them a Treebo room that moved three times in an afternoon -- went
    out as "6 rooms changed price", above a list where the same room name
    appeared three times. The reader can see the repeat, and is then reading a
    headline they know to be wrong, which costs more than the number.

    So the count is distinct ``(property, room)`` pairs, matched to the
    question actually being asked: how many rooms moved.

    The move count is added ONLY when it differs. Three rooms that moved six
    times is a busier afternoon than three that moved once, and that is worth a
    reader's attention -- but on the ordinary window where each room moved
    once, "(3 moves)" beside "3 rooms" is noise restating the same number.

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
    rooms = len({(" ".join(line.hotel_name.split()), line.room_name) for line in moved})
    moves = len(moved)

    text = f"{rooms} {'room' if rooms == 1 else 'rooms'} changed price"
    if window_hours > 0:
        text += f" in the last {window_hours} {'hour' if window_hours == 1 else 'hours'}"
    if moves > rooms:
        text += f" ({moves} moves)"
    return text


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

    A variable CAN contain a newline -- see ``_SEGMENT`` for the measurement
    that overturned the opposite belief -- so a slot is a block: the property
    name on its own line and one room per line under it. A comma still cannot
    appear in a parameter (see the My Dreams provider).
    """
    slots = max(1, param_count - _FIXED_PARAMS)

    # An en dash, not a colon. An earlier template labelled each slot
    # -- "Property: ..." -- so a colon here made every line read
    # "Property: STERLING: ▼ Classic Room: ..." with three of them before the
    # first number.
    #
    # The property name is bold because WhatsApp renders *asterisks* inside a
    # PARAMETER, not only in the template's own text -- confirmed on the live
    # reseller path, which is the one that mangles things. It is the only
    # hierarchy a template variable can carry, and it is what makes the first
    # line of a block read as the heading for the lines under it rather than as
    # another row. Two characters against the slot budget.
    segments = _blocks(
        [
            (hotel, [text for line in rooms for text in _wa_room(line)])
            for hotel, rooms in _by_hotel(moved).items()
        ]
    )
    packed, dropped = _fit(segments, slots, _WHATSAPP_PARAM_BUDGET)

    if dropped:
        # Said in the count rather than appended to a slot, so the sentence the
        # reader trusts most is the one that admits what is missing.
        headline += f" ({dropped} more on the dashboard)"
    else:
        # Nothing was cut, so the slots still holding a dash are slots this
        # window genuinely had nothing to put in. See :func:`_context`.
        packed = _context(packed, moved)

    return [headline, *packed, stamp]


def _wa_room(line: ChangeLine) -> list[str]:
    """One moved room as WhatsApp lines: the name, then the figures.

    A PHONE IS FORTY CHARACTERS WIDE AND THE ONE-LINE FORM IS SIXTY
    ===============================================================
    ``_headline`` puts the room, both prices and the delta on one line. That
    reads correctly on WhatsApp Web, which is as wide as the window, and wraps
    on a handset -- where the remainder starts at column 0 and looks like a
    line of its own, so the block the newlines just bought falls apart again.

    Indentation cannot rescue a wrapped line: runs of spaces are collapsed
    before Meta sees them (measured alongside the newline), so a continuation
    cannot be set in from the margin.

    So the line is split where it can be split honestly -- the name, then the
    figures -- and every line comes in under forty characters. Chosen on the
    handset out of three candidates rather than from a character count.

    THE DELTA KEEPS ITS SIGN AND LOSES ITS WORD
    ===========================================
    "Increase"/"Decrease" spelled out is worth a lot in the text and email
    bodies, and ``_headline`` keeps it there: a bare minus is misread by
    everyone at least once. Here the direction is already said by the arrow at
    the head of the room name, so the word would be the third statement of the
    same fact -- and it is the one that pushes the figures line into a wrap.

    Availability keeps its one-line form. "Sold out" is short, and a room that
    has no price has no figures line to put underneath it.
    """
    room = _flat(line.room_name)
    # The tax note rides on the NAME line, not the figures line. It is the
    # exception -- one room whose site publishes a different basis from the
    # rest of the message -- and sixteen characters on a line that is already
    # near forty is the one thing that would wrap here. The name line has the
    # room to spare, and "this room, on a different basis" is what the note
    # means anyway.
    note = f" · {line.basis_note}" if line.basis_note else ""
    if line.direction == "became_unavailable":
        return [f"🚫 {room} — sold out{note}"]
    if line.direction == "became_available":
        return [
            f"✅ {room}{note}",
            f"available again at {money(line.new_price, line.currency)}",
        ]

    arrow = "▲" if line.direction == "increase" else "▼"
    sign = "+" if line.direction == "increase" else "-"
    figures = f"{money(line.old_price, line.currency)} → {money(line.new_price, line.currency)}"
    if line.delta is not None:
        figures += f" · {sign}{money(abs(line.delta), line.currency)} ({_pct(line.delta_pct)})"
    return [f"{arrow} {room}{note}", figures]


def _blocks(groups: list[tuple[str, list[str]]]) -> list[str]:
    """One property to a slot, its name on the first line and a room per line.

    THE SLOT IS A BLOCK, NOT A SENTENCE
    ===================================
    This message spent its whole life cramming a property and its rooms into
    one run-on line, because a template variable was believed to be unable to
    hold a newline. It can -- see ``_SEGMENT`` for the measurement -- so the
    shape a reader actually wants is available::

        *Sterling*
        ▲ Classic Room
        ₹3,218.36 → ₹3,390 · +₹171.64 (5.3%)
        ▼ Classic Room
        ₹3,228.57 → ₹3,065.10 · -₹163.47 (5.1%)

    The property name gets a line of its own rather than a dash and the first
    room after it. It is the heading for the lines beneath it, and a heading
    that shares a line with its first row is not a heading.

    The rooms are two lines each, for the reason in :func:`_wa_room`.

    Rooms stay in ONE slot with their property. The template puts a blank line
    between slots, so a room pushed into the next slot would land under that
    gap, reading as though it belonged to nothing.
    """
    return [
        "\n".join([f"*{hotel}*", *rooms])
        for hotel, rooms in groups
    ]


#: What a spare slot says before it falls back to a dash.
#:
#: Only ever said once, and only when the message is complete -- a window that
#: dropped properties for space says so in the count instead.
_NOTHING_FURTHER = "Nothing else moved in this window."


def _context(packed: list[str], moved: Sequence[ChangeLine]) -> list[str]:
    """Spend slots the window did not need on something worth reading.

    In order, because that is the order the reader wants them:

    * **The nights these rates are for.** A rate is a rate FOR a night, and
      this is the one thing the text and email bodies carry that the WhatsApp
      message never had room for -- there is no variable for it and no line
      spare on a busy window. Added only when every move shares one stay, since
      two different stays under one heading would be a wrong number.
    * **That the list is the whole list.** "Nothing else moved" is a real
      answer to the question the message is about, and it is the difference
      between a quiet market and a monitor that stopped halfway.

    Anything still spare keeps its dash. That is the last resort and it stays:
    Meta rejects an empty variable as 132005, permanently.
    """
    spare = [i for i, slot in enumerate(packed) if slot == _UNUSED_SLOT]
    if not spare or not moved:
        return packed

    lines = []
    stays = {(line.check_in, line.check_out) for line in moved}
    if len(stays) == 1:
        lines.append(f"Stay {_stay(moved[0])}")
    lines.append(_NOTHING_FURTHER)

    filled = list(packed)
    for index, line in zip(spare, lines, strict=False):
        filled[index] = line
    return filled


def _fit(segments: list[str], slots: int, budget: int) -> tuple[list[str], int]:
    """Lay properties out across ``slots`` variables, and say how many did not.

    ONE PROPERTY PER SLOT WHILE THERE ARE SLOTS TO SPARE
    ===================================================
    Packed greedily to the budget, three properties became one wall of text
    under "Rooms and properties:" while the three slots below it said "no
    further changes" -- the worst of both: an unreadable line AND three wasted
    ones. The template puts each slot on its own line, so spreading them is
    free and keeps each property at the top of a block of its own.

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
        # EVENLY, NOT GREEDILY, ONCE PACKING IS UNAVOIDABLE
        # =================================================
        # Filling each slot to the budget before starting the next put seven
        # properties into {{2}} -- one 650-character paragraph -- while {{3}},
        # {{4}} and {{5}} said "—". That is the wall of text AND the wasted
        # lines this function exists to avoid, arriving by the other door: the
        # spread above only covers windows with a slot per property, and this
        # deployment watches ten.
        #
        # So each slot takes its share of what is left, recomputed every time
        # round: when the budget cuts a slot short, the properties it could not
        # hold are re-divided over the slots that remain rather than lost.
        share = -(-(len(segments) - index) // (slots - len(filled)))
        kept: list[str] = []
        used = 0
        while index < len(segments) and len(kept) < share:
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
