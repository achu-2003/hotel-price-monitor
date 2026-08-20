"""The Alerts page: registering a recipient, and who they actually cover.

The distinction these pin down is the one that costs money. Creating a
``recipients`` row tells nobody anything -- the dispatcher reads
``hotel_recipients`` -- so a person with no assignment is registered and inert,
and looks identical to a broken alert pipeline from the outside. Every test
here is about making that state visible, or about refusing to offer a channel
this deployment cannot actually send on.
"""
from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.dashboard.routes import templates


class _Request:
    """Enough of a Request for base.html, which only reads the path."""

    url = SimpleNamespace(path="/notifications")


def _recipient(**overrides):
    base = {
        "id": 1,
        "name": "Priya",
        "email": "priya@example.com",
        "phone_e164": "+919876543210",
        "timezone": "Asia/Kolkata",
        "is_active": True,
        "quiet_hours_start": None,
        "quiet_hours_end": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _link(**overrides):
    base = {
        "id": 1,
        "hotel_id": 7,
        "recipient_id": 1,
        "channels": ["email"],
        "min_delta_abs": None,
        "min_delta_pct": None,
        "is_active": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def render(**overrides) -> str:
    context = {
        "request": _Request(),
        "user": SimpleNamespace(email="ops@example.com", full_name="Ops"),
        "is_admin": True,
        "attention": {"total": 0},
        "recipients": [_recipient()],
        "assignments": {1: [(_link(), "Sunrise Resort")]},
        "hotels": [SimpleNamespace(id=7, name="Sunrise Resort")],
        "channels": ["email", "whatsapp"],
        "default_quiet": (time(22, 0), time(7, 0)),
        "notifications": [],
        "hours": 168,
    }
    context.update(overrides)
    return templates.get_template("notifications.html").render(**context)


class TestUnassignedRecipients:
    """The failure mode the page exists to surface."""

    def test_a_recipient_covering_nothing_is_flagged_not_left_blank(self):
        page = render(assignments={})
        assert "Nothing will ever be sent to this" in page
        # A blank cell reads as "fine"; the pill is what makes it a question.
        assert 'title="This person receives nothing"' in page

    def test_an_assigned_recipient_names_the_hotel(self):
        page = render()
        assert "Sunrise Resort" in page
        assert "This person receives nothing" not in page


class TestChannelsOffered:
    """Never offer a channel this deployment cannot send on."""

    def test_unconfigured_channels_are_not_assignable(self):
        page = render(channels=["email"])
        assert 'value="whatsapp"' not in page
        assert 'value="email"' in page

    def test_no_channel_at_all_says_so_before_the_form_is_filled_in(self):
        page = render(channels=[])
        assert "No channel is configured on this deployment" in page

    def test_whatsapp_test_button_is_dead_without_a_number(self):
        """The provider would fail with no_address; better to say it here."""
        page = render(recipients=[_recipient(phone_e164=None)])
        assert "No phone number on file" in page

    def test_email_test_button_is_dead_without_an_address(self):
        page = render(recipients=[_recipient(email=None)])
        assert "No email address on file" in page


class TestQuietHours:
    def test_an_unset_window_shows_the_deployment_default(self):
        page = render()
        assert "default 22:00–07:00" in page

    def test_a_persons_own_window_replaces_it(self):
        page = render(
            recipients=[
                _recipient(quiet_hours_start=time(23, 30), quiet_hours_end=time(6, 0))
            ]
        )
        assert "23:30–06:00" in page
        assert "default 22:00" not in page


class TestThresholds:
    def test_no_threshold_reads_as_everything_rather_than_blank(self):
        page = render()
        assert "anything" in page

    def test_a_threshold_is_shown_in_the_currency_and_the_percentage(self):
        page = render(
            assignments={
                1: [(_link(min_delta_abs=Decimal("500"), min_delta_pct=Decimal("5")), "Sunrise Resort")]
            }
        )
        assert "500" in page
        assert "5%" in page


def create_form(page: str) -> str:
    """Just the registration form, so a field name matching elsewhere on the
    page -- the row's own editor uses the same names -- cannot pass a test."""
    start = page.index('class="stack create-recipient"')
    return page[start:page.index("</form>", start)]


class TestRegistrationForm:
    """Contact details, and nothing else.

    Hotels, channels, quiet hours and thresholds are settings someone tunes
    once and revisits; asking for them while typing in a phone number turns a
    three-field job into a form people put off. They live on the row instead.
    """

    @pytest.mark.parametrize("field", ["name", "email", "phone_e164"])
    def test_it_asks_how_to_reach_the_person(self, field):
        assert 'name="' + field + '"' in create_form(render())

    @pytest.mark.parametrize(
        "field", ["hotels", "channels", "timezone", "quiet_hours_start",
                  "quiet_hours_end", "min_delta_abs", "min_delta_pct"],
    )
    def test_it_asks_for_nothing_else(self, field):
        assert 'name="' + field + '"' not in create_form(render())

    def test_the_phone_field_does_not_refuse_a_number_as_people_write_it(self):
        """Storage is E.164; typing is not.

        A ``pattern`` here made the browser reject 9876543210 before anything
        could look at it, which is how most people write a number they are
        reading off a card. app.js rewrites it on blur instead.
        """
        form = create_form(render())
        assert "pattern=" not in form
        assert "data-phone" in form

    def test_it_says_where_the_hotels_are_chosen(self):
        """Registering someone is half the job, and the half that sends
        nothing. The form has to point at the other half."""
        assert "Expand their row to choose the hotels" in create_form(render())


class TestPermissions:
    """A viewer sees the state; only an admin can change who is told."""

    @pytest.mark.parametrize(
        "marker",
        ["Add a recipient", "create-recipient", "assign-hotel",
         "toggle-recipient", "unassign-hotel", "test-notify",
         "assign-all-hotels"],
    )
    def test_every_mutating_control_is_admin_only(self, marker):
        assert marker in render()
        assert marker not in render(is_admin=False)


class TestDeliveryHistory:
    def test_a_held_message_says_when_it_will_go(self):
        notification = SimpleNamespace(
            id=3,
            created_at=datetime(2026, 8, 20, 2, 0, tzinfo=UTC),
            channel="email",
            provider="smtp",
            status=SimpleNamespace(value="queued"),
            scheduled_for=datetime(2026, 8, 20, 1, 30, tzinfo=UTC),
            error_detail=None,
            subject="Sunrise Resort: Deluxe Room down ₹300",
        )
        page = render(notifications=[(notification, "Priya", "Sunrise Resort")])
        assert "held →" in page
        assert "queued" in page
