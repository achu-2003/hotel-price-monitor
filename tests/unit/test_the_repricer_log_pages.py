"""What the two pages say about the rates the repricer changed.

The queries are pinned against a real database in
``tests/integration/test_the_repricer_log_shows_the_changes_first.py``; these
pin what an owner reads.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.dashboard.routes import templates


def _change(**over):
    base = dict(
        id=1, created_at=datetime(2026, 9, 24, 19, 28, tzinfo=UTC),  # 00:58 IST on the 25th
        mode="auto", status="applied", room_name="Standard Double", rms_room=None,
        plan="CP", market_price=Decimal("9500"), target_price=Decimal("9400"),
        current_rms=Decimal("9952"), proposed_rms=Decimal("8855"),
        applied_rms=Decimal("8855"), reason=None, screenshot_path=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _notifications(**over) -> str:
    ctx = dict(
        request=SimpleNamespace(url=SimpleNamespace(path="/notifications")),
        user=SimpleNamespace(username="owner", full_name="Owner"),
        is_admin=False, attention={"total": 0}, notifications=[], hours=168,
        actions=[_change()], changes_only=True, log_day=None, benchmark_name=None,
    )
    ctx.update(over)
    return templates.get_template("notifications.html").render(**ctx)


class TestTheLog:
    def test_changes_only_is_the_chosen_view(self):
        html = _notifications()
        assert 'class="chip is-on"\n         href="/notifications?hours=168&log=changes' in html

    def test_all_decisions_is_one_click_away(self):
        html = _notifications(changes_only=False)
        assert 'class="chip is-on"\n         href="/notifications?hours=168&log=all' in html

    def test_the_chosen_day_is_kept_when_switching_views(self):
        html = _notifications(log_day=date(2026, 9, 25))
        assert "log=all&day=2026-09-25" in html
        assert 'value="2026-09-25"' in html

    def test_a_change_shows_its_local_time_and_both_rates(self):
        html = _notifications()
        assert "25 Sep 00:58" in html
        assert "9,952" in html and "8,855" in html

    def test_the_market_column_names_the_benchmark(self):
        assert ">Sterling</th>" in _notifications(benchmark_name="Sterling")
        assert ">Market</th>" in _notifications()

    def test_an_empty_day_says_nothing_was_changed_that_day(self):
        html = _notifications(actions=[], log_day=date(2026, 9, 25))
        assert "No rate was changed on 25 Sep 2026." in html


def _repricing(**over) -> str:
    ctx = dict(
        request=SimpleNamespace(url=SimpleNamespace(path="/repricing")),
        user=SimpleNamespace(id=2, username="owner", is_admin=False),
        attention={"total": 0}, own=None, check_in=date(2026, 9, 25),
        last_auto_change=_change(), auto_changes_today=5, today=date(2026, 9, 25),
    )
    ctx.update(over)
    return templates.get_template("repricing.html").render(**ctx)


class TestTheRepricingPage:
    def test_the_last_automatic_change_is_named_with_its_time_and_rates(self):
        text = " ".join(_repricing().split())
        assert "Last automatic change: <strong>25 Sep 00:58</strong> — Standard Double CP" in text
        assert "9,952 → " in text and "8,855" in text

    def test_todays_count(self):
        assert "Today: 5 rates changed" in _repricing()
        assert "Today: 1 rate changed" in _repricing(auto_changes_today=1)

    def test_it_links_to_the_changes_that_day(self):
        assert "/notifications?log=changes&day=2026-09-25#repricer-log" in _repricing()

    def test_before_any_automatic_change(self):
        assert "No automatic change yet." in _repricing(last_auto_change=None, auto_changes_today=0)
