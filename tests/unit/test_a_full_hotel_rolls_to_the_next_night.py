"""A hotel with no rooms tonight still has a rate for tomorrow.

THE INCIDENT THIS FILE IS ABOUT
===============================
MGM Whispering Nest was checked for tonight -- 28 Aug -> 29 Aug -- and its
booking engine answered with the only thing on the page:

    No available rooms on the selected dates, Please select new dates !

424 characters, most of them that sentence, and the monitor filed

    parse_schema_drift - No elements matched room_card selector
    'div.bg-white.px-3.pt-3', and no sold-out phrase appears anywhere in the
    424 characters of text the page rendered. This is almost certainly a
    redesign -- see the saved screenshot.

Two separate failures, one on top of the other.

1. The phrase was missed on WORD ORDER. ``no rooms available`` had been in the
   marker list from the beginning; this engine writes the adjective first. So
   a page that stated its availability in plain English was read as markup we
   no longer understood, and every check dispatched a repair attempt at a
   booking engine that had not changed.

2. Even read correctly, the evening produced nothing. The night was full, so
   the check recorded a sold-out and stopped -- on exactly the evenings when
   the market is tightest and the next night's rate is the only live number
   in it.

What happens now: the phrase is recognised, the sold-out is recorded against
the night that was asked for, and a last-minute window is re-checked one night
later so the rate that IS on sale is captured too. Each reading is filed under
its own absolute dates. When both nights are full, that is the answer, and the
run says so instead of showing "0 offers, 0 changes".
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.base import FetchContext, FetchResult, NormalizedOffer
from app.adapters.parsing import looks_sold_out
from app.adapters.playwright_direct_site import PlaywrightDirectSiteAdapter
from app.core.errors import SchemaDriftError
from app.core.ratelimit import LockNotAcquired
from app.services.dates import StayWindow, local_today, rollover_window
from app.workers import tasks_fetch
from app.workers.tasks_fetch import (
    _has_rooms,
    _rollover_target,
    _sold_out_note,
    _with_rollover,
)

#: Verbatim from the saved artifact: the search panel and the banner, which is
#: very nearly the whole page.
THE_PAGE = (
    "Location MGM Whispering Nest, Yelagiri Check In August 28, 2026 "
    "Check Out August 29, 2026 Promo Code Adults Guest (1) 2 Adults - 0 Child "
    "Check Availability "
    "No available rooms on the selected dates, Please select new dates !"
)

TONIGHT = StayWindow(date(2026, 8, 28), date(2026, 8, 29))
TOMORROW = StayWindow(date(2026, 8, 29), date(2026, 8, 30))

CONTEXT = FetchContext(
    hotel_source_id=34,
    hotel_name="MGM WHISPERING NEST",
    url="https://mgmwhisperingnest.example/booking",
    external_id=None,
    stay=TONIGHT,
    adults=2,
    children=0,
    currency="INR",
)

CONFIG = {
    "room_card": "div.bg-white.px-3.pt-3",
    "selectors": {"room_name": "h4", "price": "span.price"},
}


class _Page:
    def __init__(self, body: str):
        self._body = body

    def query_selector_all(self, _selector) -> list:
        return []

    def inner_text(self, _selector, timeout=None) -> str:  # noqa: ARG002
        return self._body


class _Fetch:
    def __init__(self, body: str):
        self.page = _Page(body)
        self.json_responses: list = []


def room(name: str = "Deluxe", price: str = "3200") -> NormalizedOffer:
    return NormalizedOffer(raw_room_name=name, price_inclusive=Decimal(price))


def sold_out_result() -> FetchResult:
    return FetchResult(offers=[], sold_out_detected=True)


class TestThePhraseThatWasMissed:
    def test_the_banner_announces_no_availability(self):
        assert looks_sold_out(
            "No available rooms on the selected dates, Please select new dates !"
        )

    def test_the_singular_is_caught_too(self):
        assert looks_sold_out("There is no available room for these dates.")

    def test_the_page_is_read_as_sold_out_not_as_a_redesign(self):
        offers, sold_out = PlaywrightDirectSiteAdapter()._extract_dom(
            _Fetch(THE_PAGE), CONFIG, CONTEXT
        )

        assert sold_out is True
        assert offers == []

    def test_a_page_that_really_was_redesigned_still_raises(self):
        """Widening the marker list is only safe while the other answer is
        still reachable."""
        with pytest.raises(SchemaDriftError):
            PlaywrightDirectSiteAdapter()._extract_dom(
                _Fetch("Deluxe Room 3,200 Book Now"), CONFIG, CONTEXT
            )

    def test_ordinary_booking_copy_is_not_a_sold_out(self):
        """A marker has to ANNOUNCE unavailability. Inventing one writes a
        confident business fact and tells whoever watches that hotel."""
        assert not looks_sold_out(
            "No rooms selected yet. Choose a room to continue your booking."
        )


class TestWhichNightIsTriedNext:
    TODAY = date(2026, 8, 28)

    def test_tonight_rolls_to_tomorrow_night(self):
        assert rollover_window(TONIGHT, today=self.TODAY) == TOMORROW

    def test_tomorrow_rolls_to_the_night_after(self):
        assert rollover_window(TOMORROW, today=self.TODAY) == StayWindow(
            date(2026, 8, 30), date(2026, 8, 31)
        )

    def test_a_window_beyond_the_horizon_does_not_move(self):
        """A target watching a fixed night in November is asking about THAT
        night. Sold out is the answer, and pricing the 24th instead would
        answer a question nobody asked."""
        november = StayWindow(date(2026, 11, 23), date(2026, 11, 24))

        assert rollover_window(november, today=self.TODAY) is None

    def test_the_length_of_the_stay_is_preserved(self):
        three_nights = StayWindow(date(2026, 8, 28), date(2026, 8, 31))

        assert rollover_window(three_nights, today=self.TODAY) == StayWindow(
            date(2026, 8, 29), date(2026, 9, 1)
        )

    def test_zero_switches_rolling_off(self):
        assert rollover_window(TONIGHT, today=self.TODAY, horizon_days=0) is None


class TestWhenARollIsAllowedAtAll:
    TODAY = date(2026, 8, 28)

    def _target(self, result: FetchResult):
        return _rollover_target(result, TONIGHT, today=self.TODAY, horizon_days=1)

    def test_a_sold_out_night_rolls(self):
        assert self._target(sold_out_result()) == TOMORROW

    def test_a_night_with_rooms_does_not(self):
        assert self._target(FetchResult(offers=[room()])) is None

    def test_a_listing_where_every_room_is_unavailable_rolls(self):
        """An eZee page prints three room types with "Not Available" under
        each. Three offers, nothing for sale."""
        gone = [
            NormalizedOffer(raw_room_name=name, is_available=False)
            for name in ("Deluxe", "Suite", "Cottage")
        ]

        assert self._target(FetchResult(offers=gone, sold_out_detected=True)) == TOMORROW

    def test_an_unexplained_empty_result_never_rolls(self):
        """The failure this system refuses to make: hiding a broken selector
        behind a night of prices from a different date."""
        assert self._target(FetchResult(offers=[], sold_out_detected=False)) is None


class _Adapter:
    """Records the windows it was asked for, and answers from a script."""

    def __init__(self, *results):
        self.results = list(results)
        self.windows: list[StayWindow] = []

    def fetch(self, context: FetchContext) -> FetchResult:
        self.windows.append(context.stay)
        answer = self.results.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


SETTINGS = SimpleNamespace(
    browser_locale="en-IN",
    browser_timezone="Asia/Kolkata",
    sold_out_rollover_days=1,
)

PAYLOAD = {
    "hotel_id": 1,
    "hotel_name": "MGM WHISPERING NEST",
    "hotel_source_id": 34,
    "source_id": 3,
    "adapter_key": "playwright_direct_site",
    "url": "https://mgmwhisperingnest.example/booking",
    "external_id": None,
    "currency": "INR",
    "adapter_config": CONFIG,
    "adults": 2,
    "children": 0,
    "rooms": 1,
    "meal_plan_filter": None,
    "target_ids": [11],
    "rate_limit_per_min": 6,
}


@contextmanager
def _granted(_name, _ttl):
    yield "token"


@pytest.fixture
def budget_allows(monkeypatch):
    """The politeness budget, granted, and the rolled night free to read.

    Both are patched because both talk to Redis. Leaving either real would make
    this file pass or fail on whether a server happens to be up.
    """
    monkeypatch.setattr(tasks_fetch, "effective_rate_per_min", lambda *a: 6)
    monkeypatch.setattr(
        tasks_fetch,
        "take_token",
        lambda *a: SimpleNamespace(allowed=True, retry_after_seconds=0.0),
    )
    monkeypatch.setattr(tasks_fetch, "dispatch_lock", _granted)


def roll(adapter, stay, result):
    return _with_rollover(
        adapter,
        PAYLOAD,
        stay,
        result,
        settings=SETTINGS,
        check_run_id="run-1",
        logger=tasks_fetch.log,
    )


class TestReadingTheNextNight:
    """``local_today`` is consulted inside, so these windows are built from it
    rather than pinned to a date the calendar has since moved past."""

    @property
    def tonight(self) -> StayWindow:
        today = local_today("Asia/Kolkata")
        return StayWindow(today, today + timedelta(days=1))

    @property
    def tomorrow(self) -> StayWindow:
        return StayWindow(
            self.tonight.check_in + timedelta(days=1),
            self.tonight.check_out + timedelta(days=1),
        )

    def test_a_full_night_is_followed_by_the_next_one(self, budget_allows):
        adapter = _Adapter(FetchResult(offers=[room(price="4100")]))

        readings = roll(adapter, self.tonight, sold_out_result())

        assert len(readings) == 2
        assert adapter.windows == [self.tomorrow]

    def test_the_sold_out_night_is_still_recorded(self, budget_allows):
        """The roll ADDS a reading. It never substitutes one -- tomorrow's rate
        in tonight's series is a price move that never happened."""
        readings = roll(
            _Adapter(FetchResult(offers=[room()])), self.tonight, sold_out_result()
        )

        stay, result = readings[0]
        assert stay == self.tonight
        assert result.sold_out_detected and result.offers == []

    def test_each_reading_keeps_its_own_dates(self, budget_allows):
        readings = roll(
            _Adapter(FetchResult(offers=[room()])), self.tonight, sold_out_result()
        )

        assert readings[1][0] == self.tomorrow

    def test_a_night_with_rooms_is_not_followed_by_anything(self, budget_allows):
        adapter = _Adapter()

        readings = roll(adapter, self.tonight, FetchResult(offers=[room()]))

        assert len(readings) == 1
        assert adapter.windows == []

    def test_the_rate_limit_stops_the_extra_read(self, monkeypatch):
        """The second read is a bonus. It never borrows against the budget."""
        monkeypatch.setattr(tasks_fetch, "effective_rate_per_min", lambda *a: 6)
        monkeypatch.setattr(
            tasks_fetch,
            "take_token",
            lambda *a: SimpleNamespace(allowed=False, retry_after_seconds=30.0),
        )
        monkeypatch.setattr(tasks_fetch, "dispatch_lock", _granted)
        adapter = _Adapter(FetchResult(offers=[room()]))

        readings = roll(adapter, self.tonight, sold_out_result())

        assert len(readings) == 1
        assert adapter.windows == []


class TestTheRolledNightIsLockedToo:
    """The lock this task holds is keyed on the window that was ASKED for. A
    second target watching the night the roll reads holds a DIFFERENT key and
    is free to run at the same moment -- two browsers at one hotel, and two
    transactions inserting the same price_series primary key, one of which
    loses its entire fetch to the unique violation."""

    @property
    def tonight(self) -> StayWindow:
        today = local_today("Asia/Kolkata")
        return StayWindow(today, today + timedelta(days=1))

    def _locks(self, monkeypatch) -> list[str]:
        taken: list[str] = []

        @contextmanager
        def recording(name, _ttl):
            taken.append(name)
            yield "token"

        monkeypatch.setattr(tasks_fetch, "effective_rate_per_min", lambda *a: 6)
        monkeypatch.setattr(
            tasks_fetch, "take_token",
            lambda *a: SimpleNamespace(allowed=True, retry_after_seconds=0.0),
        )
        monkeypatch.setattr(tasks_fetch, "dispatch_lock", recording)
        return taken

    def test_the_lock_names_the_night_being_read(self, monkeypatch):
        taken = self._locks(monkeypatch)
        tomorrow = self.tonight.check_in + timedelta(days=1)

        roll(_Adapter(FetchResult(offers=[room()])), self.tonight, sold_out_result())

        assert len(taken) == 1
        assert tomorrow.isoformat() in taken[0]
        assert self.tonight.check_in.isoformat() not in taken[0]

    def test_it_matches_the_name_that_night_s_own_target_would_use(self, monkeypatch):
        """Same string as DueGroup.lock_key, or the lock guards nothing."""
        taken = self._locks(monkeypatch)

        roll(_Adapter(FetchResult(offers=[room()])), self.tonight, sold_out_result())

        rolled = StayWindow(self.tonight.check_in + timedelta(days=1),
                            self.tonight.check_out + timedelta(days=1))
        expected = tasks_fetch._lock_name({**PAYLOAD,
                                           "check_in": rolled.check_in.isoformat(),
                                           "check_out": rolled.check_out.isoformat()})
        assert taken[0] == f"lock:{expected}"

    def test_a_held_lock_skips_the_roll_without_failing(self, monkeypatch):
        """Somebody else is already reading that night; theirs is the reading
        to keep. The sold-out we came with is still returned."""
        monkeypatch.setattr(tasks_fetch, "effective_rate_per_min", lambda *a: 6)
        monkeypatch.setattr(
            tasks_fetch, "take_token",
            lambda *a: SimpleNamespace(allowed=True, retry_after_seconds=0.0),
        )

        @contextmanager
        def held(_name, _ttl):
            raise LockNotAcquired("already running")
            yield  # pragma: no cover

        monkeypatch.setattr(tasks_fetch, "dispatch_lock", held)
        adapter = _Adapter(FetchResult(offers=[room()]))

        readings = roll(adapter, self.tonight, sold_out_result())

        assert len(readings) == 1
        assert adapter.windows == []
        assert readings[0][1].sold_out_detected

    def test_a_failure_on_the_extra_read_never_loses_the_first(self, budget_allows):
        """A fetch that succeeded must not be turned into a failure by the
        optional half of it."""
        adapter = _Adapter(SchemaDriftError("the next night's page broke"))

        readings = roll(adapter, self.tonight, sold_out_result())

        assert len(readings) == 1
        assert readings[0][1].sold_out_detected


class TestWhatTheRunSays:
    def test_an_ordinary_run_says_nothing(self):
        """A note that appears on every row is one nobody reads."""
        assert _sold_out_note([(TONIGHT, FetchResult(offers=[room()]))]) is None

    def test_both_nights_full_is_stated_as_such(self):
        note = _sold_out_note([(TONIGHT, sold_out_result()),
                               (TOMORROW, sold_out_result())])

        assert "28 Aug" in note and "30 Aug" in note
        assert "sold out on both nights" in note

    def test_a_successful_roll_names_the_night_it_priced(self):
        note = _sold_out_note([
            (TONIGHT, sold_out_result()),
            (TOMORROW, FetchResult(offers=[room("Deluxe"), room("Suite")])),
        ])

        assert note.startswith("No available rooms for 28 Aug")
        assert "2 room(s) on sale for 29 Aug" in note

    def test_a_full_night_that_could_not_be_rolled_still_says_so(self):
        assert _sold_out_note([(TONIGHT, sold_out_result())]) == (
            "No available rooms for 28 Aug → 29 Aug."
        )


def test_the_sold_out_flag_is_about_the_night_that_was_asked_for():
    """The flag on the check run answers "was the window I configured
    available", not "did this run find a price anywhere"."""
    assert _has_rooms(sold_out_result()) is False
    assert _has_rooms(FetchResult(offers=[room()])) is True
    assert _has_rooms(
        FetchResult(offers=[NormalizedOffer(raw_room_name="Deluxe", is_available=False)])
    ) is False
