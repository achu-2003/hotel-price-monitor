"""RMS's "Initializing System ..." box is waited out, not mistaken for a broken toolbar.

On 26 Oct 2026 it took 40 seconds to over 90 after sign-in, and every Preview
and Apply stopped at "could not find the Room Rate Manager icon".
"""
from __future__ import annotations

import pytest

from app.services import rate_app_rms
from app.services.rate_app_rms import GridError


class _Link:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def wait_for(self, *, state, timeout):
        self.page.waits += 1
        if self.page.waits <= self.page.starting_for:
            raise TimeoutError("not visible")
        if not self.page.toolbar:
            raise TimeoutError("not visible")


class _Page:
    def __init__(self, *, starting_for: int, toolbar: bool = True):
        self.starting_for, self.toolbar, self.waits = starting_for, toolbar, 0

    def set_viewport_size(self, _size):
        pass

    def wait_for_timeout(self, _ms):
        pass

    def locator(self, _selector):
        return _Link(self)

    def inner_text(self, _selector, timeout=None):
        return "Initializing System ..." if self.waits <= self.starting_for else "Dashboard"


class _Reached(Exception):
    """Past the toolbar: the rest of open_grid is not this test's business."""


@pytest.fixture(autouse=True)
def stop_after_the_toolbar(monkeypatch):
    def clicked(self, timeout=None):
        raise _Reached
    monkeypatch.setattr(_Link, "click", clicked, raising=False)


def test_a_slow_start_is_waited_out():
    page = _Page(starting_for=4)  # four 20-second waits: 80 seconds of start-up
    with pytest.raises(_Reached):
        rate_app_rms.open_grid(page, channel="Booking.com")


def test_a_start_that_never_ends_says_so(monkeypatch):
    monkeypatch.setattr(rate_app_rms, "_STARTUP_SECONDS", 0)
    with pytest.raises(GridError, match="Initializing System"):
        rate_app_rms.open_grid(_Page(starting_for=99), channel="Booking.com")


def test_a_missing_icon_after_start_up_is_still_a_missing_icon():
    with pytest.raises(GridError, match="Room Rate Manager icon"):
        rate_app_rms.open_grid(_Page(starting_for=0, toolbar=False), channel="Booking.com")
