"""Settings: registering a recipient, and who they actually cover.

These panels lived on Alerts until the delivery history -- the thing
somebody opens when a hotel says it never heard -- ended up below three
hundred lines of setup forms. The setup moved to /settings; the history
stayed. One test here still renders the history, and says so.

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

from app.dashboard.routes import _people_not_just_numbers, templates


class _Request:
    """Enough of a Request for base.html, which only reads the path."""

    url = SimpleNamespace(path="/settings")


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
        "receives_ops_alerts": False,
        "alerts_all_hotels": False,
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
        "user": SimpleNamespace(username="ops", full_name="Ops"),
        "is_admin": True,
        "attention": {"total": 0},
        "recipients": [_recipient()],
        "assignments": {1: [(_link(), "Sunrise Resort")]},
        "hotels": [SimpleNamespace(id=7, name="Sunrise Resort")],
        # How much has been sent to each person, which the delete
        # confirmation names before it destroys it.
        "sent_counts": {1: 48},
        "channels": ["email", "whatsapp"],
        "default_quiet": (time(22, 0), time(7, 0)),
        "notifications": [],
        "hours": 168,
        # The WhatsApp alert numbers panel. Mirrors what notifications_page
        # passes; override to render the panel with numbers already saved.
        "alert_numbers": [],
        "max_alert_numbers": 5,
        # Alert sensitivity. cheapest/dearest are the portfolio's extremes,
        # which the panel uses to show what the floors mean in practice.
        "alert_defaults": SimpleNamespace(
            min_delta_abs=Decimal("50.00"),
            min_delta_pct=Decimal("2.00"),
            confirm_checks=2,
            cheapest_room=Decimal("1736"),
            dearest_room=Decimal("16950"),
        ),
    }
    context.update(overrides)
    return templates.get_template("settings.html").render(**context)


def render_history(**overrides) -> str:
    """The Alerts page, which kept the delivery history and nothing else."""
    context = {
        "request": SimpleNamespace(url=SimpleNamespace(path="/notifications")),
        "user": SimpleNamespace(username="ops", full_name="Ops"),
        "is_admin": True,
        "attention": {"total": 0},
        "notifications": [],
        "hours": 168,
    }
    context.update(overrides)
    return templates.get_template("notifications.html").render(**context)


class TestAlertSensitivity:
    """One number: how far a price has to move before anybody is told.

    The comparison engine also carries a percentage floor and requires BOTH to
    be cleared. It is deliberately not on this page: an operator asking for
    "tell me about 100 rupees" and getting silence on a 25,000 rupee suite --
    because 0.4% is under a 2% floor -- has been told something untrue by a
    form. Saving here sets the percentage to zero, so the rupee amount is the
    whole rule and the page means what it says.
    """

    def test_the_amount_is_editable(self):
        assert 'name="min_delta_abs"' in render()

    def test_the_percentage_is_not_offered(self):
        """Two floors, one box, is how a setting becomes a lie."""
        assert 'name="min_delta_pct"' not in alert_sensitivity_panel(render())

    def test_the_confirm_count_is_editable(self):
        """The debounce that stops one odd reading becoming a message."""
        assert 'name="confirm_checks"' in render()

    def test_the_stored_amount_is_shown_not_a_placeholder(self):
        assert 'value="50"' in render()

    def test_a_portfolio_with_no_prices_yet_still_renders(self):
        """A fresh deployment has no series, so both extremes are None."""
        page = render(alert_defaults=SimpleNamespace(
            min_delta_abs=Decimal("50.00"), min_delta_pct=Decimal("2.00"),
            confirm_checks=2, cheapest_room=None, dearest_room=None))
        assert 'name="min_delta_abs"' in page


def alert_sensitivity_panel(page: str) -> str:
    """Just that panel.

    The recipients table further down carries its own per-assignment
    thresholds, percentage included, and a naive search of the whole page
    would find those and pass a test that means nothing.
    """
    start = page.index("alert-defaults-form")
    return page[start:page.index("</form>", start)]


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


class TestRemovingSomebody:
    """Two ways to stop telling a person things, and only one is reversible.

    Deactivating keeps the row and the delivery log; deleting takes both. The
    page has to make that difference visible BEFORE the click, because after
    it there is nothing left to look at.
    """

    def test_both_are_offered(self):
        page = render()
        assert "toggle-recipient" in page
        assert "delete-recipient" in page

    def test_the_delete_says_what_goes_with_the_person(self):
        """A confirmation that cannot say how much it destroys is asking
        somebody to agree to an unknown."""
        page = render(sent_counts={1: 48})
        assert "48 messages" in page
        assert "delete-recipient" in page and 'data-sent="48"' in page

    def test_it_points_at_the_reversible_option(self):
        assert "deactivating does" in render()

    def test_a_person_nothing_was_ever_sent_to_claims_no_messages(self):
        page = render(sent_counts={})
        assert "messages already" not in page
        assert 'data-sent="0"' in page

    def test_the_count_reaches_the_button_the_dialog_reads(self):
        """The dialog is built from the data attributes, so a count that
        renders in the paragraph but not on the button is a dialog that
        undersells what the click will do."""
        page = render(sent_counts={1: 3})
        assert 'data-sent="3"' in page
        assert 'data-hotels="1"' in page


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
         "assign-all-hotels", "delete-recipient"],
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
        page = render_history(notifications=[(notification, "Priya", "Sunrise Resort")])
        assert "held →" in page
        assert "queued" in page


def alert_number_rows(page: str) -> str:
    """Just the live rows.

    Excludes the <template> the Add button clones, which contains a row of the
    same shape and would otherwise be counted as a visible one.
    """
    start = page.index('class="alert-number-rows"')
    return page[start:page.index("alert-number-row-template", start)]


class TestAlertNumbersGrowOnDemand:
    """One row, then a button — not five empty boxes.

    Five blank rows implied five numbers were expected and made the panel look
    like a form to fill in. The list starts at the size it actually is and
    grows only when asked.
    """

    def test_an_empty_panel_offers_exactly_one_row(self):
        page = render(alert_numbers=[])

        assert alert_number_rows(page).count('class="alert-number-row"') == 1

    def test_saved_numbers_each_get_their_own_row(self):
        page = render(
            alert_numbers=[
                _recipient(id=1, name="Front office", phone_e164="+919000000001"),
                _recipient(id=2, name="Manager", phone_e164="+919000000002"),
            ]
        )

        assert alert_number_rows(page).count('class="alert-number-row"') == 2
        assert "+919000000001" in page
        assert "+919000000002" in page

    def test_no_blank_row_is_appended_after_saved_numbers(self):
        """The Add button is how a sixth field appears, not an always-on blank.

        A trailing empty row would be submitted as an empty phone and silently
        skipped, which reads as the form losing a number.
        """
        page = render(alert_numbers=[_recipient(id=1, phone_e164="+919000000001")])

        assert alert_number_rows(page).count('class="alert-number-row"') == 1

    def test_the_add_button_and_its_ceiling_are_present(self):
        page = render(alert_numbers=[])

        assert "alert-number-add" in page
        assert 'data-max="5"' in page

    def test_every_row_can_be_removed(self):
        page = render(alert_numbers=[_recipient(id=1, phone_e164="+919000000001")])

        assert "alert-number-remove" in page

    def test_a_row_template_is_shipped_for_the_add_button(self):
        """Cloned by the JS, so both row shapes cannot drift apart."""
        page = render(alert_numbers=[])

        assert "alert-number-row-template" in page

    def test_a_number_used_as_its_own_name_is_not_repeated_in_the_name_box(self):
        """Numbers saved before the name was required are named for their digits.

        Echoing that back would look like the operator had typed the number
        twice, and would answer a question the field now insists on asking.
        """
        page = render(
            alert_numbers=[_recipient(id=1, name="+919000000001",
                                      phone_e164="+919000000001")]
        )

        assert page.count("+919000000001") == 1


class TestEveryNumberSaysWhoseItIs:
    """Two fields, both of them answers: the number, and whose it is.

    The second was "Label (optional)", and optional is how a list of five
    anonymous numbers happens -- unprunable by anyone who did not type it,
    because the first question asked of a number that should stop receiving is
    whose it is. The API refuses one without a name; these pin the form that
    feeds it.
    """

    def test_the_field_asks_whose_number_it_is(self):
        assert "Whose number" in alert_number_rows(render())

    def test_it_is_not_offered_as_an_optional_label(self):
        rows = alert_number_rows(render())

        assert "Label" not in rows
        assert "(optional)" not in rows

    def test_the_cloned_row_asks_the_same_question(self):
        """The Add button clones the template; a row that lost the field there
        would be submitted nameless and refused by the API."""
        page = render()
        template = page[page.index("alert-number-row-template"):]

        assert "Whose number" in template


class TestTheTwoWaysOfAddingSomebodySitSideBySide:
    """The quick route and the considered one are alternatives, not a sequence.

    Stacked, "Add a recipient" sat below the alert-numbers panel and its own
    intro paragraph, far enough down to read as a different subject. Paired,
    the choice between them is the first thing on the page.
    """

    def test_both_panels_share_one_row(self):
        page = render()

        assert 'class="setup-pair"' in page
        assert page.count('class="setup-col"') == 2

    def test_the_alert_numbers_panel_is_in_the_pair(self):
        page = render()
        pair = page[page.index('class="setup-pair"'):]

        assert pair.index('id="alert-numbers"') < pair.index("</section>")

    def test_the_add_recipient_panel_is_in_the_pair(self):
        page = render()
        pair = page[page.index('class="setup-pair"'):]

        assert pair.index('id="add-recipient"') < pair.index("</section>")

    def test_alert_numbers_comes_first(self):
        """Left is the quick route; the drawing put it there and so does the
        reading order for anyone on a narrow screen, where the pair stacks."""
        page = render()

        assert page.index('id="alert-numbers"') < page.index('id="add-recipient"')

    def test_the_recipients_table_is_not_in_the_pair(self):
        """Six columns and a horizontal scroller already: half the page would
        guarantee one."""
        page = render()
        pair_end = page.index("</section>", page.index('class="setup-pair"'))

        assert page.index("grid-recipients") > pair_end

    def test_the_markup_is_balanced(self):
        """The pair added two divs across what used to be two sections.

        An unclosed div here does not fail loudly -- it silently swallows the
        recipients table into the right-hand column.
        """
        from html.parser import HTMLParser

        void = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr"}

        class _Check(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.errors = []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack:
                    self.errors.append(f"</{tag}> with nothing open")
                    return
                if self.stack[-1] != tag:
                    self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
                    if tag in self.stack:
                        while self.stack and self.stack.pop() != tag:
                            pass
                    return
                self.stack.pop()

        check = _Check()
        check.feed(render())

        assert check.errors == []
        assert check.stack == []


class TestSomebodyHasToHearWhenMonitoringBreaks:
    """The only switch on this page that is not about price moves.

    Every other misconfiguration here announces itself eventually -- an alert
    arrives late, or to the wrong person, and somebody says so. This one is
    discovered by nobody. With no ops contact the alert that a scraper has
    stopped is composed, finds no recipient, writes a line to a log file and is
    dropped, while the dashboard goes on showing yesterday's prices.

    It had already happened on this deployment -- "Price monitoring has gone
    quiet on 24 sources", addressed to no one -- and the control had been
    dropped from the page entirely, so there was no way to fix it from the app.
    """

    def test_the_switch_is_on_the_page(self):
        assert 'name="receives_ops_alerts"' in render()

    def test_it_reflects_a_recipient_who_already_has_it(self):
        page = render(recipients=[_recipient(receives_ops_alerts=True)])
        box = page.split('name="receives_ops_alerts"')[1][:160]
        assert "checked" in box

    def test_the_page_says_so_when_nobody_is_listening(self):
        assert "Nobody is told when monitoring breaks" in render()

    def test_it_stops_saying_so_once_somebody_is(self):
        page = render(recipients=[_recipient(receives_ops_alerts=True)])
        assert "Nobody is told when monitoring breaks" not in page

    def test_a_contact_with_no_address_does_not_count(self):
        # Ops alerts are email-only: a business-initiated WhatsApp template is
        # the wrong carrier for "your monitoring is down". Ticking the box on a
        # phone-only contact reaches nobody, so it must not silence the notice.
        page = render(recipients=[_recipient(email=None, receives_ops_alerts=True)])
        assert "Nobody is told when monitoring breaks" in page

    def test_and_that_person_is_told_why_on_their_own_row(self):
        page = render(recipients=[_recipient(email=None, receives_ops_alerts=True)])
        assert "has no email address" in page

    def test_an_inactive_contact_does_not_count_either(self):
        page = render(recipients=[_recipient(is_active=False, receives_ops_alerts=True)])
        assert "Nobody is told when monitoring breaks" in page


class TestTheDeleteOnASavedNumber:
    """A saved row carries what the x needs to delete it for real.

    Without the id on the row the button has nothing to address, and it falls
    back to what it used to do: take the row off the screen and leave the
    number receiving until somebody remembers to press Save.
    """

    def _row(self, page):
        return page.split('class="alert-number-row"')[1][:400] if             'class="alert-number-row"' in page else ""

    def test_a_saved_row_carries_its_id(self):
        page = render(alert_numbers=[
            SimpleNamespace(id=4, name="Front office", phone_e164="+919876543210")
        ])
        assert 'data-recipient-id="4"' in page

    def test_it_carries_whose_number_it_is_for_the_confirmation(self):
        page = render(alert_numbers=[
            SimpleNamespace(id=4, name="Front office", phone_e164="+919876543210")
        ])
        assert 'data-name="Front office"' in page

    def test_an_empty_starter_row_carries_no_id(self):
        # Nothing saved yet, so nothing to delete -- the x must stay a plain
        # form control rather than firing a request at a row that does not
        # exist on the server. Scoped to the numbers form: recipients further
        # down the page carry the same attribute for their own buttons.
        page = render(alert_numbers=[])
        form = page.split("alert-numbers-form")[1].split("</form>")[0]
        assert "data-recipient-id" not in form

    def test_the_template_row_cloned_by_add_carries_no_id_either(self):
        page = render(alert_numbers=[
            SimpleNamespace(id=4, name="Front office", phone_e164="+919876543210")
        ])
        template = page.split("alert-number-row-template")[1][:500]
        assert "data-recipient-id" not in template


class TestANumberIsNotAPerson:
    """The alert-numbers box must not populate the recipients table.

    Both write to the same table -- an alert number is a recipient wearing
    ``alerts_all_hotels`` -- so a number typed at the top of the page arrived
    as a row at the bottom of it too, with an empty "Hotels covered" column
    and the ``none`` badge, which says this person receives nothing. On an
    alert number that is the exact opposite of the truth: it receives every
    change on every hotel, including hotels added later.
    """

    def test_a_number_added_upstairs_is_not_listed_as_a_recipient(self):
        number = _recipient(id=9, name="Front office", email=None,
                            alerts_all_hotels=True)

        assert _people_not_just_numbers([number], {}) == []

    def test_an_ordinary_recipient_is_kept(self):
        person = _recipient(id=1)

        assert _people_not_just_numbers([person], {}) == [person]

    def test_somebody_who_is_both_keeps_their_row(self):
        """Their hotels can only be edited here.

        Dropping the row would hide a person who is being messaged behind a
        panel that cannot show a single one of their assignments.
        """
        both = _recipient(id=1, alerts_all_hotels=True)

        kept = _people_not_just_numbers([both], {1: [(_link(), "Sunrise Resort")]})

        assert kept == [both]

    def test_a_removed_number_does_not_come_back_as_a_recipient(self):
        """The complaint that produced this rule.

        A number was added, then removed, and appeared in the table below as
        an inactive recipient: no email, no hotels, the "none" badge, and
        nothing on the screen saying where it had come from. Removal
        deactivates rather than deletes -- the delivery history has to stay
        answerable -- so the flag is what remembers the row was never a
        person, and it is read here whether the row is active or not.
        """
        retired = _recipient(id=9, name="eeereer", email=None,
                             is_active=False, alerts_all_hotels=True)

        assert _people_not_just_numbers([retired], {}) == []

    def test_an_ordinary_recipient_who_was_deactivated_is_still_listed(self):
        """Somebody switched off is still somebody, and switching them back on
        is only possible from the row this would have hidden."""
        person = _recipient(id=2, is_active=False)

        assert _people_not_just_numbers([person], {}) == [person]

    def test_the_row_of_somebody_who_is_both_says_so(self):
        page = render(recipients=[_recipient(id=1, alerts_all_hotels=True)])

        assert "alert number" in recipients_table(page)

    def test_an_ordinary_row_carries_no_such_badge(self):
        assert "alert number" not in recipients_table(render())


def recipients_table(page: str) -> str:
    """Just the table.

    The panel above it is headed "WhatsApp alert numbers", so a search of the
    whole page finds that heading and passes whatever it was asked.
    """
    start = page.index("grid-recipients")
    return page[start:page.index("</table>", start)]
