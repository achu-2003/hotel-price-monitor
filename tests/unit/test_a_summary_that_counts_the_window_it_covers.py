"""The message that arrives on a clock: how many rooms moved, and by how much.

Every ``summary_interval_hours``, one message per recipient: the count for
that window, then every room that changed, grouped by property, with the old
price, the new one and the delta. It is a SECOND message. The per-change alert
still goes out the moment a move is confirmed, and nothing in this file may
change that.

WHAT IS ACTUALLY AT RISK HERE
=============================
Four things, and none of them announces itself when it breaks:

* **The window.** "4 rooms in the last 2 hours" is the sentence the reader
  acts on. A window that drifts, overlaps, or grows because the market was
  quiet turns a true count into a confident wrong one.
* **The count.** The point is ONE message rather than four. A version that
  batched per hotel would still deliver every move, four times over, and look
  correct in every log line.
* **The template.** A business-initiated WhatsApp message must use a template
  Meta approved, and the approved price-change template's seven variables mean
  hotel/room/old/new/delta/dates/time -- ONE room. Putting a whole window
  through it is accepted, charged for, and delivered saying something else. So
  the wrong template must be refused rather than substituted.
* **The per-change alert.** Adding a summary must not quieten, delay or
  deduplicate the immediate alerts. A move worth telling somebody about is
  still worth telling them about the moment it is confirmed.

WHAT THIS MESSAGE DELIBERATELY DOES NOT CARRY
=============================================
The comparison grid. It was in an earlier draft and was taken out: a message
about a window is about what CHANGED in it, and printing every room of every
property beside four moves buries the four. /comparison answers the other
question on a screen with room for a table.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db.models import (
    Hotel,
    HotelRecipient,
    Notification,
    PriceChange,
    PriceSeries,
    Recipient,
    RoomType,
)
from app.notifications import render
from app.notifications.base import (
    MARKET_COMPARISON,
    PRICE_CHANGE,
    WHATSAPP_COMPARISON_PARAM_COUNT,
    ChangeLine,
    RenderedMessage,
    whatsapp_template_for,
)
from app.notifications.digest import summary_dedupe_key
from app.notifications.providers.whatsapp_mydreams import MyDreamsWhatsAppProvider
from app.workers import tasks_notify

from tests.unit.test_email_delivery import FakeSession, RecordingProvider

#: The deployment's own clock, which is what the windows are aligned to.
IST = ZoneInfo("Asia/Kolkata")

NOW = datetime(2026, 9, 5, 15, 30, tzinfo=UTC)


def _line(hotel, room, old, new, *, direction=None):
    old_price, new_price = Decimal(old), Decimal(new)
    delta = new_price - old_price
    return ChangeLine(
        hotel_name=hotel, room_name=room,
        old_price=old_price, new_price=new_price,
        delta=delta,
        delta_pct=(delta / old_price * 100).quantize(Decimal("0.01")),
        currency="INR",
        direction=direction or ("increase" if delta > 0 else "decrease"),
        check_in="2026-09-05", check_out="2026-09-06",
    )


#: Four rooms across two properties -- the shape the requirement describes.
MOVED = [
    _line("Sunrise Resort", "Deluxe Room", "3000", "2700"),
    _line("Sunrise Resort", "Garden Villa", "5000", "5600"),
    _line("Hilltop Retreat", "Club Room", "4811", "4500"),
    _line("Hilltop Retreat", "Family Suite", "9010", "9500"),
]


def _message(moved=None, hours=2, **kw):
    return render.render_summary(
        MOVED if moved is None else moved,
        window_hours=hours,
        when=NOW,
        **kw,
    )


class TestTheMessageIsTheWindow:
    """The count, and the rooms behind it. Nothing else."""

    def test_the_count_is_the_first_thing_said(self):
        """The sentence the reader acts on. On a quiet evening it says the
        market is asleep and nothing needs doing, which is a decision made
        before a single figure is read."""
        message = _message()
        assert message.subject == "4 rooms changed price in the last 2 hours"
        assert message.text.startswith("📊 4 rooms changed price in the last 2 hours")

    def test_one_room_is_not_called_rooms(self):
        """This is the subject line. A message that cannot conjugate looks
        automated enough to ignore."""
        assert _message(MOVED[:1]).subject == "1 room changed price in the last 2 hours"

    def test_one_hour_is_not_called_hours(self):
        assert "in the last 1 hour" in _message(hours=1).subject

    def test_the_window_named_is_the_one_covered_not_the_one_configured(self):
        """A worker down for six hours sends a six-hour window on its next
        tick. Saying "the last 2 hours" over six hours of movement would be a
        wrong number stated confidently."""
        assert "in the last 6 hours" in _message(hours=6).subject

    def test_every_moved_room_is_named_with_its_prices(self):
        text = _message().text
        for room in ("Deluxe Room", "Garden Villa", "Club Room", "Family Suite"):
            assert room in text
        assert "₹3,000" in text and "₹2,700" in text

    def test_the_delta_is_spelled_out_not_left_as_a_sign(self):
        """Everyone who has read one of these has read a bare minus the wrong
        way at least once, and the two directions call for opposite
        responses."""
        text = _message().text
        assert "Decrease ₹300" in text and "10.0%" in text
        assert "Increase ₹600" in text

    def test_moves_are_grouped_under_their_property(self):
        """A window can span four properties, and a flat list repeats the
        property name on every line -- the wrapping that made the per-hotel
        digest unreadable on a phone."""
        text = _message().text
        assert text.count("Sunrise Resort") == 1
        assert text.count("Hilltop Retreat") == 1

    def test_the_stay_dates_travel_with_each_move(self):
        """A rate is a rate FOR a night. A delta with no night attached is a
        number the reader cannot act on."""
        assert "05 Sep 2026" in _message().text

    def test_the_comparison_grid_is_not_in_it(self):
        """Deliberate, and the reason this class replaced one that checked the
        opposite. Rooms that did not move are not in a message about what
        moved."""
        text = _message().text
        for absent in ("below us", "above us", "we do not sell this tier", "OURS —"):
            assert absent not in text

    def test_the_html_carries_every_move(self):
        html = _message().html
        assert "<table" in html
        for room in ("Deluxe Room", "Garden Villa", "Club Room", "Family Suite"):
            assert room in html

    def test_it_declares_which_template_it_belongs_to(self):
        assert _message().kind == MARKET_COMPARISON


class TestTheWhatsAppTemplateContract:
    """A template variable is a contract, and a broken one is paid for."""

    def test_the_parameter_count_is_the_one_the_template_declares(self):
        assert len(_message().template_params) == WHATSAPP_COMPARISON_PARAM_COUNT

    def test_no_parameter_contains_a_newline(self):
        """Meta rejects a newline inside a template variable as 132005, which
        is permanent -- so the moves arrive as run-on text."""
        for param in _message(param_count=5).template_params:
            assert "\n" not in param and "\t" not in param

    def test_no_parameter_is_empty(self):
        """An empty variable is 132005 as well."""
        for param in _message(param_count=5).template_params:
            assert param.strip()

    def test_the_count_leads_and_the_time_ends(self):
        """The order is a contract with whatever Meta approved. The fixed two
        sit at the ends, so a bigger template is the same message with more
        room in it rather than a different message."""
        small = _message(param_count=3).template_params
        large = _message(param_count=7).template_params
        assert small[0] == large[0] == "4 rooms changed price in the last 2 hours"
        assert small[-1] == large[-1]
        assert len(small) == 3 and len(large) == 7

    def test_a_bigger_template_carries_more_of_the_window(self):
        """The reason the count is configuration and not a constant. Meta caps
        a single body variable and both providers cut one at 700 characters,
        so a busy afternoon does not fit in one."""
        many = [
            _line(f"Property Number {n} Resort and Spa", f"Deluxe Room {n}",
                  str(5000 + n * 10), str(5400 + n * 10))
            for n in range(30)
        ]
        small = _message(many, param_count=3)
        large = _message(many, param_count=8)
        assert len("".join(large.template_params)) > len("".join(small.template_params))

    def test_what_did_not_fit_is_admitted_in_the_count(self):
        """Said in the sentence the reader trusts most, rather than appended to
        a slot they may never reach."""
        many = [
            _line(f"Property Number {n} Resort and Spa", f"Deluxe Room {n}",
                  str(5000 + n * 10), str(5400 + n * 10))
            for n in range(30)
        ]
        assert "more on the dashboard" in _message(many, param_count=3).template_params[0]

    def test_a_slot_the_window_did_not_fill_is_never_empty(self):
        """Meta rejects an empty variable as 132005, which is permanent -- so a
        quiet window on a large template must not become an undeliverable
        alert."""
        message = _message(MOVED[:1], param_count=8)
        assert len(message.template_params) == 8
        for param in message.template_params:
            assert param.strip()

    def test_a_trim_never_cuts_a_figure_in_half(self):
        """A rate cut in half by a character count is a wrong number presented
        as a right one, which is the one thing these messages may not do."""
        many = [
            _line(f"Property Number {n} Resort and Spa", f"Deluxe Room {n}",
                  str(5000 + n * 10), str(5400 + n * 10))
            for n in range(30)
        ]
        for param in _message(many, param_count=4).template_params:
            assert not param.endswith("₹")
            assert len(param) <= 700

    def test_the_summary_and_the_price_change_use_different_templates(self):
        settings = SimpleNamespace(
            whatsapp_template_name="rate_update_alert",
            whatsapp_comparison_template_name="market_comparison_alert",
            whatsapp_comparison_template_params=5,
        )
        assert whatsapp_template_for(PRICE_CHANGE, settings)[0] == "rate_update_alert"
        assert (
            whatsapp_template_for(MARKET_COMPARISON, settings)[0]
            == "market_comparison_alert"
        )

    def test_an_unapproved_summary_template_is_refused_not_substituted(self, monkeypatch):
        """The dangerous failure is not "no message" -- it is a whole window
        sent through the price-change template, which Meta accepts, charges
        for, and delivers reading like an alert about one room that never
        moved."""
        provider = MyDreamsWhatsAppProvider()
        settings = SimpleNamespace(
            whatsapp_enabled=True,
            mydreams_license_number="123",
            mydreams_api_key=SimpleNamespace(get_secret_value=lambda: "k"),
            mydreams_base_url="https://example.invalid/api",
            whatsapp_template_name="rate_update_alert",
            whatsapp_comparison_template_name="",       # not approved yet
            whatsapp_comparison_template_params=5,
        )
        monkeypatch.setattr(
            "app.notifications.providers.whatsapp_mydreams.get_settings",
            lambda: settings,
        )

        result = provider.send(
            SimpleNamespace(name="Priya", email=None, phone_e164="+919876543210"),
            _message(),
        )
        assert result.ok is False
        assert result.error_code == "template_not_configured"
        assert result.retryable is False


# -- the dispatcher ---------------------------------------------------
def _world(*, hotels, links, changes, series, rooms, recipients):
    return FakeSession(
        hotels=hotels, recipients=recipients, links=links,
        changes=changes, series=series, rooms=rooms,
    )


def _four_hotel_world():
    """One person, two properties of ours, and a move on each.

    Two hotels rather than one, because the whole claim of the switch is that
    the reader gets ONE message instead of one per property.
    """
    owned = [
        Hotel(id=7, name="Sunrise Resort", owner_user_id=42),
        Hotel(id=8, name="Hilltop Retreat", owner_user_id=42),
    ]
    recipient = Recipient(
        id=1, name="Priya", email="priya@example.com", phone_e164=None,
        timezone="Asia/Kolkata", is_active=True,
        quiet_hours_start=None, quiet_hours_end=None,
    )
    links = [
        HotelRecipient(id=1, hotel_id=7, recipient_id=1, channels=["email"],
                       min_delta_abs=None, min_delta_pct=None, is_active=True),
        HotelRecipient(id=2, hotel_id=8, recipient_id=1, channels=["email"],
                       min_delta_abs=None, min_delta_pct=None, is_active=True),
    ]
    changes = [
        PriceChange(
            id=99, hotel_id=7, offer_key="offer-1",
            old_price=Decimal("3000"), new_price=Decimal("2700"),
            delta=Decimal("-300"), delta_pct=Decimal("-10.00"),
            currency="INR", direction="decrease",
            previous_offer_key=None, notified=False,
            changed_at=NOW - timedelta(minutes=30),
        ),
        PriceChange(
            id=100, hotel_id=8, offer_key="offer-2",
            old_price=Decimal("5000"), new_price=Decimal("5600"),
            delta=Decimal("600"), delta_pct=Decimal("12.00"),
            currency="INR", direction="increase",
            previous_offer_key=None, notified=False,
            changed_at=NOW - timedelta(minutes=10),
        ),
    ]
    series = [
        PriceSeries(offer_key="offer-1", room_type_id=3,
                    check_in=date(2026, 9, 5), check_out=date(2026, 9, 6),
                    meal_plan=None),
        PriceSeries(offer_key="offer-2", room_type_id=4,
                    check_in=date(2026, 9, 5), check_out=date(2026, 9, 6),
                    meal_plan=None),
    ]
    rooms = [
        RoomType(id=3, hotel_id=7, name="Deluxe Room"),
        RoomType(id=4, hotel_id=8, name="Garden Villa"),
    ]
    return _world(hotels=owned, links=links, changes=changes,
                  series=series, rooms=rooms, recipients=[recipient])


@pytest.fixture
def world(monkeypatch):
    session = _four_hotel_world()

    @contextmanager
    def fake_sync_session():
        yield session

    monkeypatch.setattr(tasks_notify, "sync_session", fake_sync_session)
    monkeypatch.setattr(
        tasks_notify.send_notification, "apply_async",
        lambda *args, **kwargs: session.tables.setdefault("queued", []).append(args),
    )
    monkeypatch.setattr(
        tasks_notify.registry, "get_provider", lambda channel: RecordingProvider()
    )
    monkeypatch.setattr(tasks_notify, "recipient_quota_remaining", lambda *a: 10)
    monkeypatch.setattr(tasks_notify, "consume_recipient_quota", lambda *a: None)
    # A two-hour interval and a grid, which is what most of these tests want.
    # The ones that do not re-call _configure themselves.
    _configure(monkeypatch)
    # The window is whatever slot NOW falls after, and FakeSession ignores the
    # date filter anyway -- so the changes above are simply "what was in it".
    monkeypatch.setattr(tasks_notify, "datetime", _FrozenClock)
    return session


class _FrozenClock(datetime):
    """``datetime.now`` pinned, so the window is the same one every run.

    Subclassed rather than mocked with a lambda: the task also does arithmetic
    on what ``now()`` returns, and a bare stub would fail there rather than
    where the test is looking.
    """

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def _configure(monkeypatch, *, hours: int = 2):
    """The interval the task reads."""
    monkeypatch.setattr(
        tasks_notify.monitoring_service, "summary_interval_hours", lambda: hours
    )


def _rows(session):
    return session.tables.get("notifications", [])


class TestASecondTickInTheSameWindowIsHeld:
    """The dedupe key is the only thing stopping a repeat send.

    The task keeps no watermark by design: it recomputes the last closed window
    on every tick and offers the same message until one is written. With a
    five-minute beat and a two-hour interval that is roughly twenty-four
    offers per window, twenty-three of which MUST be swallowed.

    So the swallow has to be silent and total. This class exists because it was
    neither: the insert was added to the session before the savepoint was
    opened, ``Session.begin_nested()`` flushed it as it took its snapshot, and
    the duplicate landed on the OUTER transaction. The task then died with
    PendingRollbackError, and every recipient after the first was lost with it
    -- once per window, for the whole life of the deployment.
    """

    def test_the_second_tick_writes_nothing(self, world, monkeypatch):
        tasks_notify.market_summary()
        first = len(_rows(world))
        assert first > 0

        tasks_notify.market_summary()
        assert len(_rows(world)) == first

    def test_a_duplicate_does_not_take_the_run_down_with_it(self, world, monkeypatch):
        """The savepoint has to cover the INSERT, not just the flush.

        A duplicate that poisons the transaction loses every row still to be
        written -- on a two-channel assignment, the WhatsApp message goes with
        the email one. Two channels, so the first collides and the second must
        still be attempted.
        """
        world.tables["links"][1].channels = ["email", "whatsapp"]
        tasks_notify.market_summary()
        channels = {row.channel for row in _rows(world)}
        assert channels == {"email", "whatsapp"}

        # Again, in the same window: both are duplicates now, and neither the
        # exception nor a partial write may escape.
        tasks_notify.market_summary()
        assert {row.channel for row in _rows(world)} == {"email", "whatsapp"}
        assert len(_rows(world)) == 2

    def test_a_new_window_is_not_held(self, world, monkeypatch):
        """The other half. A dedupe that swallowed the NEXT window too would
        make the feature send exactly one message, ever."""
        tasks_notify.market_summary()
        sent = len(_rows(world))

        # Two hours later: a different slot, so a different key.
        later = NOW + timedelta(hours=2)
        monkeypatch.setattr(
            tasks_notify, "datetime", type("C", (datetime,), {
                "now": classmethod(lambda cls, tz=None: later if tz is None
                                   else later.astimezone(tz))})
        )
        tasks_notify.market_summary()
        assert len(_rows(world)) > sent


class TestTheWindowIsAClosedSlotOnTheClock:
    """When "the last two hours" starts and stops.

    Nothing else in the feature can be right if this is wrong: the count is a
    statement about a window, and a window that drifts makes every count a
    confident wrong answer.

    Slots are aligned to midnight in the DEPLOYMENT's timezone, not in UTC, and
    these tests are written in it for that reason. A reader in Kolkata is told
    "the last two hours" and expects the boundaries to be 8, 10, 12 on their
    own clock; aligning to UTC would put them at 5:30 and 7:30 and make every
    stated hour a half-hour out.

    Midnight alignment is also what lets the task keep no state at all -- every
    tick recomputes the same window from the clock, and the dedupe key is the
    window.
    """

    #: Ticks are written on the deployment's own clock. See the class docstring.
    def _at(self, hour, minute=0, day=8):
        return datetime(2026, 9, day, hour, minute, tzinfo=IST)

    def _local(self, moment):
        return moment.astimezone(IST)

    def test_the_window_reported_is_the_last_one_that_closed(self):
        """Never the one in progress. A window reported half-full would
        undercount, and the missing moves would never be reported by anything:
        the next tick reports the next slot."""
        start, end = tasks_notify._last_closed_window(self._at(14, 3), 2)
        assert self._local(start).hour == 12
        assert self._local(end).hour == 14

    def test_every_tick_inside_one_slot_names_the_same_window(self):
        """The task ticks every five minutes and the window is its only
        identity. Two ticks that disagreed would send the same summary twice
        under two different dedupe keys."""
        windows = {
            tasks_notify._last_closed_window(self._at(14, minute), 2)
            for minute in range(0, 60, 7)
        }
        assert len(windows) == 1

    def test_consecutive_slots_do_not_overlap_or_leave_a_gap(self):
        """A room that moved must be counted once and never dropped. An
        overlap double-counts it in two summaries; a gap loses it from both."""
        first = tasks_notify._last_closed_window(self._at(12, 30), 2)
        second = tasks_notify._last_closed_window(self._at(14, 30), 2)
        assert first[1] == second[0]

    def test_the_first_tick_of_the_day_reports_last_nights_slot(self):
        """Not an empty window, and not a slot from the day before that. The
        movement at 11 PM is read at 8 AM."""
        start, end = tasks_notify._last_closed_window(self._at(0, 30), 2)
        assert self._local(end).day == 8 and self._local(end).hour == 0
        assert self._local(start).day == 7 and self._local(start).hour == 22

    def test_an_interval_that_does_not_divide_a_day_reports_its_short_slot(self):
        """Five hours leaves four before midnight. The message names the hours
        it actually covered -- saying "the last 5 hours" over four hours of
        movement is the kind of confident wrong number this system exists to
        prevent."""
        start, end = tasks_notify._last_closed_window(self._at(2, 5), 5)
        assert self._local(end).hour == 0
        assert tasks_notify._window_hours(start, end) == 4

    def test_a_whole_slot_is_reported_as_its_whole_length(self):
        """The short slot above is the exception, and it must stay one."""
        start, end = tasks_notify._last_closed_window(self._at(14, 3), 5)
        assert tasks_notify._window_hours(start, end) == 5

    def test_two_ticks_in_one_slot_produce_one_identity(self):
        """The task keeps no watermark. What stops a second send is the dedupe
        key, and the key is the window -- so this equality IS the guarantee,
        enforced by the unique index on notifications.dedupe_key."""
        first = tasks_notify._last_closed_window(self._at(14, 3), 2)[0]
        second = tasks_notify._last_closed_window(self._at(15, 58), 2)[0]
        assert summary_dedupe_key(1, "whatsapp", first) == summary_dedupe_key(
            1, "whatsapp", second
        )

    def test_a_different_slot_is_a_different_identity(self):
        """Otherwise the second window of the day would be swallowed as a
        duplicate of the first and nobody would ever hear it."""
        early = tasks_notify._last_closed_window(self._at(12, 3), 2)[0]
        later = tasks_notify._last_closed_window(self._at(14, 3), 2)[0]
        assert summary_dedupe_key(1, "whatsapp", early) != summary_dedupe_key(
            1, "whatsapp", later
        )


class TestTheSummaryIsOneMessagePerPerson:
    def test_two_properties_moving_produce_one_message(self, world, monkeypatch):
        """The whole claim. Two properties moved and the reader gets one
        counted window, not the same table twice."""
        tasks_notify.market_summary()

        rows = _rows(world)
        assert len(rows) == 1
        assert rows[0].kind == MARKET_COMPARISON

    def test_the_one_message_accounts_for_every_change(self, world, monkeypatch):
        """Both changes are on the row, so the delivery history still answers
        "was this move in a summary?" for each of them."""
        tasks_notify.market_summary()
        assert sorted(_rows(world)[0].price_change_ids) == [99, 100]

    def test_it_is_filed_under_no_single_hotel(self, world, monkeypatch):
        """It is about a window across the portfolio. Filing it under whichever
        property happened to move first would put one property's name on a
        table that is not about it."""
        tasks_notify.market_summary()
        assert _rows(world)[0].hotel_id is None

    def test_the_count_leads_the_message(self, world, monkeypatch):
        """The sentence the reader acts on. On a quiet evening the count alone
        says the market is asleep and nothing needs doing, which is a decision
        made before any table is read."""
        tasks_notify.market_summary()
        row = _rows(world)[0]
        assert "2 rooms changed price in the last 2 hours" in row.subject
        assert "2 rooms changed price in the last 2 hours" in row.body_rendered

    def test_the_rooms_that_moved_are_named(self, world, monkeypatch):
        """A count with no list is a number the reader cannot act on without
        opening the dashboard -- which is the work this message exists to have
        already done."""
        body = (tasks_notify.market_summary(), _rows(world)[0].body_rendered)[1]
        assert "Deluxe Room" in body and "Garden Villa" in body
        assert "Sunrise Resort" in body and "Hilltop Retreat" in body

    def test_only_the_rooms_that_moved_are_in_it(self, world, monkeypatch):
        """The requirement in one assertion. A window is about what CHANGED in
        it, so a room nobody repriced has no line -- and neither does a
        competitor's untouched rate."""
        tasks_notify.market_summary()
        body = _rows(world)[0].body_rendered
        assert "Deluxe Room" in body and "Garden Villa" in body
        for absent in ("below us", "above us", "we do not sell this tier"):
            assert absent not in body

    def test_a_quiet_window_sends_nothing(self, world, monkeypatch):
        """Silence means no room moved. A message saying so every two hours
        around the clock is a paid WhatsApp that teaches its reader to ignore
        the number."""
        world.tables["changes"] = []
        tasks_notify.market_summary()
        assert _rows(world) == []

    def test_an_interval_of_zero_sends_nothing(self, world, monkeypatch):
        """Off is where every deployment starts, and it must cost nothing --
        not a query, not a grid, not a message."""
        _configure(monkeypatch, hours=0)
        tasks_notify.market_summary()
        assert _rows(world) == []

    def test_each_move_carries_its_old_and_new_price(self, world, monkeypatch):
        """"How much" is half the requirement. A count with no figures is a
        number the reader has to open the dashboard to act on -- which is the
        work this message exists to have already done."""
        tasks_notify.market_summary()
        body = _rows(world)[0].body_rendered
        assert "₹3,000" in body and "₹2,700" in body
        assert "Decrease ₹300" in body

    def test_every_channel_the_person_is_assigned_on_is_used(self, world, monkeypatch):
        """The same person can hold email on one property and email+WhatsApp on
        another. The one message covering both has to go everywhere either
        assignment asked for -- dropping a channel here silences a number that
        was reaching them yesterday."""
        world.tables["links"][1].channels = ["email", "whatsapp"]
        tasks_notify.market_summary()

        assert {row.channel for row in _rows(world)} == {"email", "whatsapp"}


class TestTheSameFiltersAsTheImmediateAlert:
    """A summary that counted moves the reader was never told about would
    disagree with their own inbox, and the inbox is what they trust."""

    def test_a_change_under_the_threshold_is_not_counted(self, world, monkeypatch):
        for link in world.tables["links"]:
            link.min_delta_abs = Decimal("100000")

        tasks_notify.market_summary()
        assert _rows(world) == []

    def test_an_inactive_recipient_hears_nothing(self, world, monkeypatch):
        world.tables["recipients"][0].is_active = False
        tasks_notify.market_summary()
        assert _rows(world) == []

    def test_an_inactive_assignment_hears_nothing(self, world, monkeypatch):
        for link in world.tables["links"]:
            link.is_active = False
        tasks_notify.market_summary()
        assert _rows(world) == []

    def test_the_summary_does_not_mark_the_changes_notified(self, world, monkeypatch):
        """``notified`` belongs to the per-change alert. A summary that set it
        would silence the immediate message for every move it happened to read
        first -- the summary would arrive and the alert never would."""
        for change in world.tables["changes"]:
            change.notified = False

        tasks_notify.market_summary()
        assert not any(c.notified for c in world.tables["changes"])


class TestTheImmediateAlertIsUntouched:
    """The summary is a second message. Every one of these passed before it
    existed and must go on passing."""

    def test_a_confirmed_change_still_sends_one_message_per_hotel(
        self, world, monkeypatch
    ):
        tasks_notify.dispatch_changes([99, 100])

        rows = _rows(world)
        assert len(rows) == 2
        assert {r.hotel_id for r in rows} == {7, 8}
        assert {r.kind for r in rows} == {PRICE_CHANGE}

    def test_it_still_marks_the_changes_notified(self, world, monkeypatch):
        """Otherwise they reappear in every subsequent dispatch forever."""
        tasks_notify.dispatch_changes([99, 100])
        assert all(c.notified for c in world.tables["changes"])

    def test_it_does_not_consult_the_interval_at_all(self, world, monkeypatch):
        """The per-change alert must not acquire a dependency on a setting that
        can be zero. Reading it here is how "summaries off" would one day mean
        "no alerts at all"."""

        def explode():
            raise AssertionError("dispatch_changes read the summary interval")

        monkeypatch.setattr(
            tasks_notify.monitoring_service, "summary_interval_hours", explode
        )
        tasks_notify.dispatch_changes([99, 100])
        assert len(_rows(world)) == 2


class TestAHeldMessageComesBackAsWhatItWas:
    def test_a_summary_rebuilds_as_a_summary(self, world, monkeypatch):
        """Rebuilt from the row rather than from the setting. A summary queued
        at 11 PM must be released at 7 AM as a summary even if somebody set the
        interval to zero overnight -- and it must reach WhatsApp on the
        template it was queued for, because the other one would take its
        parameters and mean something else."""
        _configure(monkeypatch)
        notification = Notification(
            id=1, recipient_id=1, hotel_id=None, channel="email",
            provider="smtp", kind=MARKET_COMPARISON, dedupe_key="d",
            price_change_ids=[99, 100], subject="s", body_rendered="b",
            created_at=NOW,
        )
        # Summaries are switched off by the time the hold is released.
        monkeypatch.setattr(
            tasks_notify.monitoring_service, "summary_interval_hours", lambda: 0
        )

        message = tasks_notify._rebuild_message(world, notification, "s", "b")
        assert message.kind == MARKET_COMPARISON
        assert "Deluxe Room" in message.text

    def test_the_window_it_reported_is_replayed_not_recounted(
        self, world, monkeypatch
    ):
        """What moved is history and cannot change; the grid is a statement
        about now and must be rebuilt. A summary released at 7 AM still says
        what the 11 PM window contained."""
        _configure(monkeypatch)
        notification = Notification(
            id=1, recipient_id=1, hotel_id=None, channel="email",
            provider="smtp", kind=MARKET_COMPARISON, dedupe_key="d",
            price_change_ids=[99, 100], subject="s", body_rendered="b",
            created_at=NOW,
        )

        message = tasks_notify._rebuild_message(world, notification, "s", "b")
        assert "2 rooms changed price" in message.text
        assert "Deluxe Room" in message.text

    def test_a_summary_with_nothing_left_keeps_the_text_it_was_queued_with(
        self, world, monkeypatch
    ):
        """The stored body is the audit record of what was said. Email still
        carries something true; WhatsApp refuses it for want of parameters,
        which is honest -- there is nothing to put in them."""
        _configure(monkeypatch)
        world.tables["changes"] = []
        notification = Notification(
            id=1, recipient_id=1, hotel_id=None, channel="email",
            provider="smtp", kind=MARKET_COMPARISON, dedupe_key="d",
            price_change_ids=[99, 100], subject="s",
            body_rendered="the table as it stood", created_at=NOW,
        )

        message = tasks_notify._rebuild_message(
            world, notification, "s", "the table as it stood"
        )
        assert isinstance(message, RenderedMessage)
        assert message.kind == MARKET_COMPARISON
        assert message.text == "the table as it stood"
        assert message.template_params is None
