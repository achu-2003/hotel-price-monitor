"""Drive RMS Cloud's Room Rate Manager: open the grid, read a rate, write a rate.

The login probe (``rate_app_login``) proves the saved details get in. This
is everything past the door, written against RMS Cloud in particular
because that is what the owner's rate application turned out to be. Three
things, composed by the callers in ``tasks_repricing``:

:func:`open_grid`      walk to Room Rate Manager, switch to the Channel view,
                       expand the channel's row so its room types show
:func:`read_rate`      one cell -- room type, rate type, day -- as a number
:func:`write_rate`     the same cell, set through the application's own
                       override dialog and read back afterwards

and :func:`survey`, the read-only visit the Rate app page's Test login
makes: every mapped row read, nothing written, a picture of the grid.

THE ROUTE, AS A PERSON WALKS IT
===============================
Top toolbar -> the line-chart icon ("Room Rate Manager", route
``#!/TariffAvailabilityChartV2``) -> the first dropdown set to "Channel"
(``vm.SelectedViewBy``) -> "+" on the channel's row (the rooms appear) ->
"+" on the room's row (its rate types appear, with a price per day) ->
right-click the day's price -> "Override Base Room Rate" -> the "Room Rate
Override" dialog, with the range preset to that one day and the price
pre-selected in "New Room Rate" -> type the new price -> the floppy icon in
the dialog's title bar, which is Save/Exit.

Two things that are not what they look like. The "+" and the channel's
NAME are different controls on the same row: the name has its own click
handler that jumps to Channel Management Setup, the "+" expands the row.
And every price in the grid is an ``<input>`` with an on-blur override of
its own, so a direct type-into-cell route exists; the dialog route is used
because it has an explicit Save and a range that says which day it is.

ROWS NEST, SO ROWS ARE FOUND INSIDE THEIR PARENT. "Online Rate CP" is a
row under CLASSIC and another under SUITE; looked for on the whole page,
the first one wins whichever room was meant. A rate row is found inside its
room's row, and a room's row inside its channel's.

WHY THE WINDOW IS WIDENED FIRST. Below about 1400px RMS hides the
dropdowns behind a "More Options" button (a layers icon beside the title)
and the "Channel" select is display:none, so it can neither be seen nor
chosen. The browser opens narrow; the page is given a desktop-sized window
before the toolbar is touched, which is how the owner's own screen shows it.

THE GRID RELOADS ITSELF AFTER A SAVE -- every cell reads N/A for a few
seconds and the rows are redrawn -- so a cell is found afresh and given time
to show a number before it is believed. Read too soon, the first version of
this reported "the grid shows no number" while RMS's own toast said saved.
"""
from __future__ import annotations

import re
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import structlog

from app.services.rate_app_login import LoginProbe, _screenshot

log = structlog.get_logger(__name__)

_VIEWPORT = {"width": 1800, "height": 1000}
_RATE_MANAGER_LINK = "a.kt-menu__toggle[href='#!/TariffAvailabilityChartV2']:visible"
_VIEW_SELECT = "select[ng-model='vm.SelectedViewBy']"
_ROW_HEADER = ".rate-manager-row-header"
_ROW_LABEL = f"{_ROW_HEADER} label"
_DATE_CELLS = ".RMSScheduleGrid-DateCell"
_OVERRIDE_MENU_ITEM = "rms-popover li"
_OVERRIDE_DIALOG = ".modal.RateOverrideController"
_OVERRIDE_INPUT = f"{_OVERRIDE_DIALOG} input[ng-model='ScreenData.TariffOverride']"
_OVERRIDE_SAVE = f"{_OVERRIDE_DIALOG} .btn-save"
_OVERRIDE_CLOSE = f"{_OVERRIDE_DIALOG} .btn-close"
#: How long a route change or a save is given before it is read.
_ROUTE_SECONDS = 20
#: How many day columns the grid shows at its default zoom.
DAYS_SHOWN = 14


class GridError(Exception):
    """The grid did not look the way the route expects. The message says where."""


def _settle(page, *, expect_text: str | None = None, seconds: float = _ROUTE_SECONDS) -> bool:
    """Wait for the network to go quiet and, if asked, for words to appear.

    RMS routes fade in behind a grey wash for a few seconds after the DOM
    is there; a screenshot taken on arrival is a grey page, and one taken
    at three seconds is a dim one. The wait ends on the words, then allows
    the fade.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=int(seconds * 1_000))
    except Exception:  # noqa: BLE001
        pass
    if expect_text is None:
        page.wait_for_timeout(1_500)
        return True
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if expect_text.lower() in page.inner_text("body", timeout=2_000).lower():
                page.wait_for_timeout(5_000)
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)
    return False


def _row_names(scope) -> list[str]:
    """The names down the left of the grid, for a report that says what was there."""
    try:
        return [" ".join(t.split()) for t in scope.locator(_ROW_LABEL).all_inner_texts() if t.strip()][:40]
    except Exception:  # noqa: BLE001
        return []


def _wait_for_rows_to_change(page, before: list[str]) -> None:
    """After the view switch the grid is redrawn from a fresh request; until
    it is, the old rows are still there. Wait for them to change, then settle."""
    deadline = time.monotonic() + _ROUTE_SECONDS
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if _row_names(page) != before:
            _settle(page)
            return


def _row_named(scope, level: int, name: str):
    """The ``.row-wrapper`` at ``level`` inside ``scope`` whose own label is ``name``.

    Level 1 is the channel, 2 the room type, 3 the rate type. Matched on the
    row's own header label, case-insensitively, whitespace folded.
    """
    rows = scope.locator(f".row-wrapper.level{level}")
    want = " ".join(name.split()).lower()
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            label = row.locator(_ROW_LABEL).first.inner_text(timeout=2_000)
        except Exception:  # noqa: BLE001
            continue
        if " ".join(label.split()).lower() == want:
            return row
    return None


def _direct_names(scope, level: int) -> list[str]:
    """The labels of the level-N rows inside scope, for a "not X but Y" report."""
    rows = scope.locator(f".row-wrapper.level{level}")
    out = []
    for i in range(min(rows.count(), 40)):
        try:
            out.append(" ".join(rows.nth(i).locator(_ROW_LABEL).first.inner_text(timeout=1_000).split()))
        except Exception:  # noqa: BLE001
            continue
    return out


def _expand(page, row) -> None:
    """Click the row's "+" unless it is already a "-"."""
    plus = row.locator(_ROW_HEADER).first.locator("i.fa-plus")
    if plus.count():
        plus.first.click(timeout=10_000)
        _settle(page)


def _digits(text: str | None) -> Decimal | None:
    """"8,000" and "Rs 8,000.00" are both 8000; N/A is nothing."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return Decimal(match.group().replace(",", "")) if match else None


def _cell_value(cell) -> Decimal | None:
    """What a grid cell shows: its input's value, or its text when it has none."""
    try:
        box = cell.locator("input")
        if box.count():
            return _digits(box.first.input_value(timeout=5_000))
    except Exception:  # noqa: BLE001
        pass
    try:
        return _digits(cell.inner_text(timeout=2_000))
    except Exception:  # noqa: BLE001
        return None


def day_heading(page, day_index: int) -> str:
    """"Thu 17 Sep" from the column heading, for the report."""
    try:
        heads = page.locator(_DATE_CELLS)
        if heads.count() > day_index:
            text = " ".join(heads.nth(day_index).inner_text(timeout=2_000).split())
            if text:
                return text
    except Exception:  # noqa: BLE001
        pass
    return f"day {day_index + 1} of the grid"


# ---------------------------------------------------------------------------


def open_grid(page, *, channel: str) -> None:
    """From the landing page to the Channel view with ``channel`` expanded.

    Raises :class:`GridError` naming the step that did not go as expected.
    """
    page.set_viewport_size(_VIEWPORT)
    page.wait_for_timeout(500)

    # The landing page draws its toolbar after the login verdict is read;
    # the icon is waited for, not looked for once.
    link = page.locator(_RATE_MANAGER_LINK).first
    try:
        link.wait_for(state="visible", timeout=_ROUTE_SECONDS * 1_000)
    except Exception:  # noqa: BLE001
        raise GridError("could not find the Room Rate Manager icon in the top toolbar")
    link.click(timeout=10_000)
    if not _settle(page, expect_text="Room Rate Manager"):
        raise GridError("Room Rate Manager did not open")

    view = page.locator(_VIEW_SELECT)
    if not view.count():
        raise GridError("Room Rate Manager opened without its view dropdown")
    before = _row_names(page)
    try:
        view.select_option(label="Channel", timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        raise GridError(f"the view dropdown would not switch to Channel ({str(exc).splitlines()[0]})")
    _wait_for_rows_to_change(page, before)

    channel_row = _row_named(page, 1, channel)
    if channel_row is None:
        listed = ", ".join(_direct_names(page, 1)) or "nothing"
        raise GridError(f"the Channel view listed {listed}, not {channel}")
    _expand(page, channel_row)


def channel_names(page) -> list[str]:
    """Every channel the Channel view lists, top to bottom."""
    return _direct_names(page, 1)


def expand_channel(page, *, channel: str) -> None:
    """Open one more channel's rows on a grid :func:`open_grid` already opened."""
    channel_row = _row_named(page, 1, channel)
    if channel_row is None:
        listed = ", ".join(channel_names(page)) or "nothing"
        raise GridError(f"the Channel view listed {listed}, not {channel}")
    _expand(page, channel_row)


def _cell(page, *, channel: str, room: str, rate_type: str, day_index: int):
    """Locate one cell, expanding the room's row if it is closed."""
    channel_row = _row_named(page, 1, channel)
    if channel_row is None:
        raise GridError(f"the {channel} row is no longer on the grid")
    room_row = _row_named(channel_row, 2, room)
    if room_row is None:
        listed = ", ".join(_direct_names(channel_row, 2)) or "no rooms"
        raise GridError(f"under {channel} the grid shows {listed}, not {room}")
    _expand(page, room_row)
    rate_row = _row_named(room_row, 3, rate_type)
    if rate_row is None:
        listed = ", ".join(_direct_names(room_row, 3)) or "no rate types"
        raise GridError(f"under {channel} / {room} the rate types are {listed}, not {rate_type}")
    cell = rate_row.locator(f".gridCell[index='{day_index}'] .cell-wrapper").first
    if not cell.count():
        raise GridError(f"the {room} / {rate_type} row has no cell for day {day_index + 1}")
    return cell


def read_rate(page, *, channel: str, room: str, rate_type: str, day_index: int = 0) -> Decimal | None:
    """The number in one cell, or ``None`` where it shows N/A."""
    return _cell_value(_cell(page, channel=channel, room=room, rate_type=rate_type, day_index=day_index))


def write_rate(page, *, channel: str, room: str, rate_type: str, day_index: int,
               amount: Decimal) -> Decimal | None:
    """Set one cell through Override Base Room Rate and return what it shows after.

    ``None`` means the dialog took the number but the grid did not show one
    afterwards -- the caller reports that as unconfirmed, not as done.
    Raises :class:`GridError` when the route breaks before the save.
    """
    cell = _cell(page, channel=channel, room=room, rate_type=rate_type, day_index=day_index)
    want = int(amount)

    cell.click(button="right", timeout=10_000)
    item = page.locator(_OVERRIDE_MENU_ITEM, has_text="Override Base Room Rate").first
    try:
        item.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        raise GridError("right-clicking the price showed no 'Override Base Room Rate' menu")
    item.click(timeout=10_000)

    box = page.locator(_OVERRIDE_INPUT).first
    try:
        box.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        raise GridError("the Room Rate Override dialog did not open")
    # The box selects all on focus; the new number is typed key by key so
    # the currency directive sees keystrokes, as the login's code box had to.
    box.click(timeout=5_000)
    box.press("Control+a")
    box.press_sequentially(str(want), delay=30)
    box.press("Tab")
    page.wait_for_timeout(500)
    typed = _digits(box.input_value())
    if typed is None or int(typed) != want:
        page.locator(_OVERRIDE_CLOSE).first.click(timeout=5_000)
        raise GridError(f"the dialog read the new rate back as {typed}, not {want}; closed it without saving")

    page.locator(_OVERRIDE_SAVE).first.click(timeout=10_000)
    try:
        page.locator(_OVERRIDE_DIALOG).first.wait_for(state="hidden", timeout=_ROUTE_SECONDS * 1_000)
    except Exception:  # noqa: BLE001
        raise GridError(f"pressed Save/Exit with {want} but the dialog stayed open")
    _settle(page)

    # See the module docstring: the grid redraws itself after a save.
    deadline = time.monotonic() + _ROUTE_SECONDS
    now = None
    while time.monotonic() < deadline:
        try:
            now = read_rate(page, channel=channel, room=room, rate_type=rate_type, day_index=day_index)
        except GridError:
            now = None
        if now is not None:
            break
        page.wait_for_timeout(500)
    page.wait_for_timeout(1_000)
    log.info("rate_app_rate_written", channel=channel, room=room, rate_type=rate_type,
             day_index=day_index, want=want, now=str(now))
    return now


# ---------------------------------------------------------------------------


def survey(page, probe: LoginProbe, *, owner_user_id: int, channel: str,
           cells: list[tuple[str, str]], day_index: int = 0,
           readings: dict | None = None) -> LoginProbe:
    """Read every ``(room, rate_type)`` in ``cells`` for one day. Writes nothing.

    The Rate app page's Test login runs this after a good login: it proves
    the route to every mapped row without moving a rate, and leaves the
    numbers in ``readings`` (keyed by the tuple) for the caller to keep.
    The probe comes back with the grid's picture in place of the landing
    page's and a sentence listing what was read.
    """
    readings = readings if readings is not None else {}
    landing_shot = probe.screenshot_path

    def finish(sentence: str) -> LoginProbe:
        shot = _screenshot(page, owner_user_id)
        _drop(landing_shot, keep=shot)
        return replace(probe, message=f"{probe.message} {sentence}", screenshot_path=shot or landing_shot,
                       landed_url=page.url, landed_title=page.title())

    try:
        open_grid(page, channel=channel)
    except GridError as exc:
        return finish(f"Then tried to open the {channel} rates but {exc}.")

    day = day_heading(page, day_index)
    found, missing = [], []
    for room, rate_type in cells:
        try:
            value = read_rate(page, channel=channel, room=room, rate_type=rate_type, day_index=day_index)
        except GridError as exc:
            missing.append(f"{room} / {rate_type} ({exc})")
            continue
        readings[(room, rate_type)] = value
        found.append(f"{room} / {rate_type} = {value:,.0f}" if value is not None else f"{room} / {rate_type} = N/A")

    sentence = f"Then read the {channel} rates for {day} without changing anything"
    if found:
        sentence += ": " + "; ".join(found)
    if missing:
        sentence += ". Could not find " + "; ".join(missing)
    if not cells:
        sentence += " -- no rooms are mapped yet, so nothing was read (map them on the Repricing page)"
    return finish(sentence + ".")


def _drop(path: str | None, *, keep: str | None) -> None:
    """The landing-page picture is superseded by the one of the grid."""
    if path and keep and path != keep:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
