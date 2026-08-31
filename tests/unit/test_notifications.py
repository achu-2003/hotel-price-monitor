"""Batching, quiet hours, thresholds, and message rendering.

These are the rules that decide whether someone's phone buzzes, and the ones
most likely to be wrong in a way nobody notices until the alerts get muted.
"""
from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.notifications.base import WHATSAPP_TEMPLATE_PARAM_COUNT, ChangeLine
from app.notifications.digest import (
    ChangeFacts,
    dedupe_key,
    group_for_digest,
    in_quiet_hours,
    is_weekend_stay,
    passes_recipient_threshold,
    release_time,
)
from app.notifications.render import money, render_digest


class TestQuietHours:
    def test_window_wraps_past_midnight(self):
        # The case a naive start <= now <= end comparison gets wrong: 22:00 to
        # 07:00 is ONE window, and the naive form reports it as never active.
        assert in_quiet_hours(time(23, 30), time(22, 0), time(7, 0)) is True
        assert in_quiet_hours(time(3, 0), time(22, 0), time(7, 0)) is True
        assert in_quiet_hours(time(12, 0), time(22, 0), time(7, 0)) is False

    def test_boundaries_are_half_open(self):
        assert in_quiet_hours(time(22, 0), time(22, 0), time(7, 0)) is True
        assert in_quiet_hours(time(7, 0), time(22, 0), time(7, 0)) is False

    def test_same_day_window(self):
        assert in_quiet_hours(time(13, 0), time(12, 0), time(14, 0)) is True
        assert in_quiet_hours(time(15, 0), time(12, 0), time(14, 0)) is False

    def test_unset_window_is_never_quiet(self):
        assert in_quiet_hours(time(3, 0), None, None) is False
        assert in_quiet_hours(time(3, 0), time(22, 0), None) is False

    def test_equal_start_and_end_is_not_a_24h_blackout(self):
        # Otherwise a misconfiguration silences someone permanently.
        assert in_quiet_hours(time(3, 0), time(9, 0), time(9, 0)) is False


class TestReleaseTime:
    def test_releases_at_the_next_occurrence_of_the_end(self):
        # 18:00 UTC is 23:30 IST — inside quiet hours, so it waits for 07:00
        # IST the next morning, which is 01:30 UTC.
        now = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)
        released = release_time(now, time(7, 0), "Asia/Kolkata")
        assert released.astimezone(ZoneInfo("Asia/Kolkata")).hour == 7
        assert released > now

    def test_returns_none_without_an_end_time(self):
        assert release_time(datetime.now(UTC), None) is None


class TestDedupeKey:
    def test_is_order_independent(self):
        """A retry must produce the same key regardless of ordering."""
        assert dedupe_key(1, "email", [3, 1, 2]) == dedupe_key(1, "email", [1, 2, 3])

    def test_differs_per_recipient_and_channel(self):
        assert dedupe_key(1, "email", [1]) != dedupe_key(2, "email", [1])
        assert dedupe_key(1, "email", [1]) != dedupe_key(1, "whatsapp", [1])

    def test_differs_per_change_set(self):
        assert dedupe_key(1, "email", [1]) != dedupe_key(1, "email", [1, 2])


class TestThresholds:
    def _facts(self, delta, pct, direction="decrease"):
        return ChangeFacts(1, 1, Decimal(delta), Decimal(pct), direction)

    def test_below_absolute_floor_is_filtered(self):
        assert not passes_recipient_threshold(
            self._facts("-30", "-3"), Decimal("50"), Decimal("2")
        )

    def test_below_percentage_floor_is_filtered(self):
        assert not passes_recipient_threshold(
            self._facts("-300", "-1"), Decimal("50"), Decimal("2")
        )

    def test_clearing_both_floors_passes(self):
        assert passes_recipient_threshold(
            self._facts("-300", "-10"), Decimal("50"), Decimal("2")
        )

    def test_sold_out_always_passes(self):
        # It has no percentage, and it is exactly what the assigned person
        # wants to know regardless of their sensitivity setting.
        facts = ChangeFacts(1, 1, None, None, "became_unavailable")
        assert passes_recipient_threshold(facts, Decimal("9999"), Decimal("99"))

    def test_no_configured_thresholds_pass_everything(self):
        assert passes_recipient_threshold(self._facts("-1", "-0.1"), None, None)


class TestGrouping:
    def test_one_batch_per_recipient_and_hotel(self):
        facts = [
            ChangeFacts(1, 10, Decimal("-100"), Decimal("-5"), "decrease"),
            ChangeFacts(2, 10, Decimal("-200"), Decimal("-8"), "decrease"),
            ChangeFacts(3, 20, Decimal("300"), Decimal("9"), "increase"),
        ]
        batches = group_for_digest(facts, {10: [1, 2], 20: [1]})

        # Recipient 1 covers both hotels and gets TWO messages, not one mixed
        # digest: each hotel is a separate decision they might act on.
        assert batches[(1, 10)] == [1, 2]
        assert batches[(1, 20)] == [3]
        assert batches[(2, 10)] == [1, 2]
        assert (2, 20) not in batches

    def test_changes_for_unassigned_hotels_are_dropped(self):
        facts = [ChangeFacts(1, 99, Decimal("-100"), Decimal("-5"), "decrease")]
        assert group_for_digest(facts, {10: [1]}) == {}


class TestMoney:
    def test_indian_digit_grouping(self):
        # ₹1,23,456 — not ₹123,456. Written the wrong way it reads as wrong to
        # the person it is written for.
        assert money(Decimal("123456"), "INR") == "₹1,23,456"
        assert money(Decimal("2500"), "INR") == "₹2,500"
        assert money(Decimal("999"), "INR") == "₹999"
        assert money(Decimal("10000000"), "INR") == "₹1,00,00,000"

    def test_western_grouping_for_other_currencies(self):
        assert money(Decimal("123456"), "USD") == "$123,456"

    def test_none_is_an_em_dash_not_zero(self):
        assert money(None) == "—"

    def test_paise_are_shown_when_present(self):
        """Booking engines really do quote half rupees.

        Aiosell returns 1202.50 for a room. Rounding that to a whole rupee made
        the dashboard disagree with the hotel's own booking page, which is the
        fastest way to lose trust in the numbers.
        """
        assert money(Decimal("1202.50")) == "₹1,202.50"
        assert money(Decimal("2177.50")) == "₹2,177.50"
        assert money(Decimal("123456.75")) == "₹1,23,456.75"

    def test_whole_amounts_show_no_decimals(self):
        assert money(Decimal("1850")) == "₹1,850"
        assert money(Decimal("1850.00")) == "₹1,850"

    def test_half_rupees_never_round_in_two_directions(self):
        """The bug this replaced: Decimal's default is banker's rounding.

        1202.50 rounded down to 1202 and 2177.50 rounded UP to 2178, so two
        identical .50 endings appeared to behave differently.
        """
        for value in ("1202.50", "2177.50", "0.50", "2.50", "3.50"):
            assert money(Decimal(value)).endswith(".50"), value

    def test_negative_keeps_its_paise(self):
        assert money(Decimal("-300.50")) == "-₹300.50"

    def test_unknown_currency_falls_back_to_the_code(self):
        assert money(Decimal("100"), "AED") == "AED 100"


class TestRenderDigest:
    def _line(self, **overrides):
        base = dict(
            hotel_name="ABC Resort",
            room_name="Deluxe Room",
            old_price=Decimal("3000"),
            new_price=Decimal("2700"),
            delta=Decimal("-300"),
            delta_pct=Decimal("-10.00"),
            currency="INR",
            direction="decrease",
            check_in="2026-08-20",
            check_out="2026-08-21",
            meal_plan=None,
        )
        base.update(overrides)
        return ChangeLine(**base)

    def test_single_change_message(self):
        message = render_digest("ABC Resort", [self._line()])
        assert "ABC Resort" in message.subject
        assert "₹3,000" in message.text
        assert "₹2,700" in message.text
        assert "10.0%" in message.text
        assert "20 Aug 2026" in message.text

    def test_sold_out_is_never_a_price_of_zero(self):
        message = render_digest(
            "ABC Resort",
            [self._line(direction="became_unavailable", new_price=None, delta=None,
                        delta_pct=None)],
        )
        assert "sold out" in message.text.lower()
        assert "100%" not in message.text
        assert "-₹3,000" not in message.text

    def test_batched_subject_counts_the_changes(self):
        lines = [self._line(), self._line(room_name="Suite")]
        message = render_digest("ABC Resort", lines)
        assert message.subject.startswith("2 price changes")
        assert "Suite" in message.text

    def test_html_escapes_room_names(self):
        """Room names come from someone else's website: untrusted input."""
        message = render_digest("ABC", [self._line(room_name="<script>x</script>")])
        assert "<script>" not in message.html
        assert "&lt;script&gt;" in message.html

    def test_whatsapp_params_match_the_approved_template_order(self):
        # Fixed by the template Meta approved: hotel, room, old, new, delta,
        # dates, time. Reordering these silently sends the wrong numbers.
        params = render_digest("ABC Resort", [self._line()]).template_params
        assert len(params) == WHATSAPP_TEMPLATE_PARAM_COUNT
        assert params[0] == "ABC Resort"
        assert params[1] == "Deluxe Room"
        assert params[2] == "₹3,000"
        assert params[3] == "₹2,700"

    def test_whatsapp_params_summarise_a_batch(self):
        params = render_digest(
            "ABC", [self._line(), self._line(room_name="Suite")]
        ).template_params
        assert "+1 more" in params[1]


def test_weekend_detection():
    assert is_weekend_stay(datetime(2026, 8, 21).date()) is True   # Friday
    assert is_weekend_stay(datetime(2026, 8, 22).date()) is True   # Saturday
    assert is_weekend_stay(datetime(2026, 8, 19).date()) is False  # Wednesday
