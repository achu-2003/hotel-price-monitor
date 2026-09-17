"""Drive RMS Cloud from the landing page to one price, and put it up by a rupee.

The login probe (``rate_app_login``) proves the saved details get in. This
is the first step past the door, written against RMS Cloud in particular
because that is what the owner's rate application turned out to be. It
walks to one cell of the Room Rate Manager grid -- one channel, one room
type, one rate type, one day -- reads the price there, writes the same
price plus one rupee through the application's own override dialog, saves,
and photographs the grid showing the new number. Small on purpose: a rupee
proves the whole path is writable without moving a rate anyone would
notice, and the picture proves it to the owner, not just to the log.

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

WHY THE WINDOW IS WIDENED FIRST. Below about 1400px RMS hides the
dropdowns behind a "More Options" button (a layers icon beside the title)
and the "Channel" select is display:none, so it can neither be seen nor
chosen. The browser opens narrow; the page is given a desktop-sized window
before the toolbar is touched, which is how the owner's own screen shows it.
"""
from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path

import structlog

from app.services.rate_app_login import LoginProbe, _screenshot

log = structlog.get_logger(__name__)

#: The channel whose price is moved. Matched case-insensitively against the
#: grid; other hotels may spell it differently or lack it, and the report
#: says so rather than opening something else.
DEFAULT_CHANNEL = "Booking.com"
#: How much to add. A rupee: enough to see, not enough to matter.
DEFAULT_DELTA = 1

_VIEWPORT = {"width": 1800, "height": 1000}
_RATE_MANAGER_LINK = "a.kt-menu__toggle[href='#!/TariffAvailabilityChartV2']:visible"
_VIEW_SELECT = "select[ng-model='vm.SelectedViewBy']"
_ROW_HEADER = ".rate-manager-row-header"
_ROW_LABEL = f"{_ROW_HEADER} label"
_OVERRIDE_MENU_ITEM = "rms-popover li"
_OVERRIDE_DIALOG = ".modal.RateOverrideController"
_OVERRIDE_INPUT = f"{_OVERRIDE_DIALOG} input[ng-model='ScreenData.TariffOverride']"
_OVERRIDE_SAVE = f"{_OVERRIDE_DIALOG} .btn-save"
_OVERRIDE_CLOSE = f"{_OVERRIDE_DIALOG} .btn-close"
#: How long a route change or a save is given before it is read.
_ROUTE_SECONDS = 20


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
        return [" ".join(t.split()) for t in scope.locator(_ROW_LABEL).all_inner_texts() if t.strip()][:20]
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


def _row_named(page, level: int, name: str | None):
    """The ``.row-wrapper`` at ``level`` whose label is ``name``, or the first one.

    Level 1 is the channel, 2 the room type, 3 the rate type. Rows nest, so
    a level-2 row is matched by its own header, not by text anywhere inside.
    """
    rows = page.locator(f".row-wrapper.level{level}")
    if name is None:
        return rows.first if rows.count() else None
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            label = row.locator(_ROW_LABEL).first.inner_text(timeout=2_000)
        except Exception:  # noqa: BLE001
            continue
        if label.strip().lower() == name.strip().lower():
            return row
    return None


def _label_of(row) -> str:
    try:
        return " ".join(row.locator(_ROW_LABEL).first.inner_text(timeout=2_000).split())
    except Exception:  # noqa: BLE001
        return "?"


def _expand(page, row) -> None:
    """Click the row's "+" unless it is already a "-"."""
    plus = row.locator(_ROW_HEADER).first.locator("i.fa-plus")
    if plus.count():
        plus.first.click(timeout=10_000)
        _settle(page)


def _digits(text: str | None) -> int | None:
    """"8,000" and "Rs 8,000.00" are both 8000; N/A is nothing."""
    match = re.search(r"\d[\d,]*", text or "")
    return int(match.group().replace(",", "")) if match else None


def _cell_value(cell) -> int | None:
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


def bump_rate(page, probe: LoginProbe, *, owner_user_id: int, channel: str = DEFAULT_CHANNEL,
              room: str | None = None, rate_type: str | None = None, day_index: int = 0,
              delta: int = DEFAULT_DELTA) -> LoginProbe:
    """From the landing page, walk to one price, raise it by ``delta``, and photograph the grid.

    ``room`` and ``rate_type`` default to the first of each under the
    channel; ``day_index`` 0 is the first column, which is today.

    Returns the probe to report: the login's verdict kept, the message
    extended with what was changed from what to what, and the screenshot
    replaced by one of the grid afterwards. A step that cannot find its way
    says which step and what it saw instead, and keeps a screenshot of where
    it stopped, so the owner can see the page the driver did not understand.
    """
    landing_shot = probe.screenshot_path

    def stopped(where: str) -> LoginProbe:
        shot = _screenshot(page, owner_user_id)
        _drop(landing_shot, keep=shot)
        return replace(
            probe,
            message=f"{probe.message} Then tried to change a {channel} rate but {where}.",
            screenshot_path=shot or landing_shot,
            landed_url=page.url, landed_title=page.title(),
        )

    page.set_viewport_size(_VIEWPORT)
    page.wait_for_timeout(500)

    # The landing page draws its toolbar after the login verdict is read;
    # the icon is waited for, not looked for once.
    link = page.locator(_RATE_MANAGER_LINK).first
    try:
        link.wait_for(state="visible", timeout=_ROUTE_SECONDS * 1_000)
    except Exception:  # noqa: BLE001
        return stopped("could not find the Room Rate Manager icon in the top toolbar")
    link.click(timeout=10_000)
    if not _settle(page, expect_text="Room Rate Manager"):
        return stopped("Room Rate Manager did not open")

    view = page.locator(_VIEW_SELECT)
    if not view.count():
        return stopped("Room Rate Manager opened without its view dropdown")
    before = _row_names(page)
    try:
        view.select_option(label="Channel", timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        return stopped(f"the view dropdown would not switch to Channel ({str(exc).splitlines()[0]})")
    _wait_for_rows_to_change(page, before)

    channel_row = _row_named(page, 1, channel)
    if channel_row is None:
        listed = ", ".join(_row_names(page)) or "nothing"
        return stopped(f"the Channel view listed {listed}, not {channel}")
    _expand(page, channel_row)

    room_row = _row_named(page, 2, room)
    if room_row is None:
        listed = ", ".join(_row_names(channel_row)) or "no rooms"
        return stopped(f"under {channel} the grid showed {listed}, not {room or 'a room type'}")
    room_name = _label_of(room_row)
    _expand(page, room_row)

    rate_row = _row_named(page, 3, rate_type)
    if rate_row is None:
        return stopped(f"under {channel} / {room_name} no rate type row appeared")
    rate_name = _label_of(rate_row)

    cell = rate_row.locator(f".gridCell[index='{day_index}'] .cell-wrapper").first
    if not cell.count():
        return stopped(f"the {rate_name} row had no cell for day {day_index + 1}")
    was = _cell_value(cell)
    if was is None:
        return stopped(f"the {rate_name} price for day {day_index + 1} is not a number")
    want = was + delta
    day = _day_heading(page, day_index)
    where = f"{channel} / {room_name} / {rate_name} for {day}"

    cell.click(button="right", timeout=10_000)
    item = page.locator(_OVERRIDE_MENU_ITEM, has_text="Override Base Room Rate").first
    try:
        item.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        return stopped("right-clicking the price showed no 'Override Base Room Rate' menu")
    item.click(timeout=10_000)

    box = page.locator(_OVERRIDE_INPUT).first
    try:
        box.wait_for(state="visible", timeout=10_000)
    except Exception:  # noqa: BLE001
        return stopped("the Room Rate Override dialog did not open")
    # The box selects all on focus; the new number is typed key by key so
    # the currency directive sees keystrokes, as the login's code box had to.
    box.click(timeout=5_000)
    box.press("Control+a")
    box.press_sequentially(str(want), delay=30)
    box.press("Tab")
    page.wait_for_timeout(500)
    typed = _digits(box.input_value())
    if typed != want:
        page.locator(_OVERRIDE_CLOSE).first.click(timeout=5_000)
        return stopped(f"the dialog read the new rate back as {typed}, not {want}; closed it without saving")

    page.locator(_OVERRIDE_SAVE).first.click(timeout=10_000)
    try:
        page.locator(_OVERRIDE_DIALOG).first.wait_for(state="hidden", timeout=_ROUTE_SECONDS * 1_000)
    except Exception:  # noqa: BLE001
        return stopped(f"pressed Save/Exit with {want} but the dialog stayed open")
    _settle(page)

    # The grid reloads itself after a save -- every cell reads N/A for a
    # few seconds and the rows are redrawn -- so the cell is found afresh
    # and given time to show a number before it is believed.
    now = None
    deadline = time.monotonic() + _ROUTE_SECONDS
    while time.monotonic() < deadline:
        rate_row = _row_named(page, 3, rate_type)
        if rate_row is not None:
            cell = rate_row.locator(f".gridCell[index='{day_index}'] .cell-wrapper").first
            now = _cell_value(cell) if cell.count() else None
            if now is not None:
                break
        page.wait_for_timeout(500)
    page.wait_for_timeout(1_000)
    shot = _screenshot(page, owner_user_id)
    _drop(landing_shot, keep=shot)
    log.info("rate_app_rate_bumped", channel=channel, room=room_name, rate_type=rate_name,
             day=day, was=was, want=want, now=now)
    if now == want:
        message = (
            f"{probe.message} Then changed a rate: {where} was {was:,}, set to {want:,} "
            f"through Override Base Room Rate and saved; the grid now shows {now:,}."
        )
    else:
        shown = f"{now:,}" if now is not None else "no number"
        message = (
            f"{probe.message} Then tried to change a rate: {where} was {was:,}, typed {want:,} "
            f"and pressed Save/Exit, but the grid shows {shown} afterwards -- check it in RMS."
        )
    return replace(probe, message=message, screenshot_path=shot or landing_shot,
                   landed_url=page.url, landed_title=page.title())


def _day_heading(page, day_index: int) -> str:
    """"Thu 17 Sep" from the column heading, for the report."""
    try:
        heads = page.locator(".RMSScheduleGrid-DateCell")
        if heads.count() > day_index:
            text = " ".join(heads.nth(day_index).inner_text(timeout=2_000).split())
            if text:
                return text
    except Exception:  # noqa: BLE001
        pass
    return f"day {day_index + 1} of the grid"


def _drop(path: str | None, *, keep: str | None) -> None:
    """The landing-page picture is superseded by the one of the grid."""
    if path and keep and path != keep:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
