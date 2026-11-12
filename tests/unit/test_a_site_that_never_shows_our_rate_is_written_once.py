"""A site that does not show our new rate is written ONCE, not every run.

12 Nov 2026, Goibibo. ASG's Deluxe Double showed at 4,473 (with tax) from
morning to evening; the rule wanted 5,525. The first run wrote CLASSIC CP
5,994 -> 7,242 (target 5,405 then). Goibibo never showed it. Every run after
took the LAST write's starting rate as the rate behind 4,473 -- which after
the first one was a rate of our own the site had never shown -- and scaled it
by the same 23 % again, twice an hour, to 17,230. DELUXE CP went to 23,066.

The rate behind the site's price is the one before the FIRST write it has not
shown. These replay that afternoon.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services import repricing as rule

D = Decimal
ON_GOIBIBO = D("4473")


def _write(before, after, our_price=ON_GOIBIBO, status="applied"):
    return SimpleNamespace(status=status, current_rms=D(before), applied_rms=D(after),
                           our_price=D(our_price))


def _next_rate(history, target, our_price=ON_GOIBIBO, cell=None):
    """One run: ``history`` oldest first, as the worker reads it newest first."""
    base = rule.ratio_base(list(reversed(history)), D(our_price)) or D(cell)
    return rule.to_rms(D(target), D(our_price), base, round_to=10, exact=True, with_tax=True)


class TestRatioBase:
    def test_nothing_written_tonight_reads_the_cell(self):
        assert rule.ratio_base([], ON_GOIBIBO) is None

    def test_one_write_the_site_has_not_shown(self):
        assert rule.ratio_base([_write("5994", "7242")], ON_GOIBIBO) == D("5994")

    def test_every_write_it_has_not_shown_goes_back_to_the_first(self):
        newest_first = [_write("8945", "9144"), _write("7403", "8945"),
                        _write("7242", "7403"), _write("5994", "7242")]
        assert rule.ratio_base(newest_first, ON_GOIBIBO) == D("5994")

    def test_a_write_the_site_did_show_ends_the_walk(self):
        """The site moved to 5,525 after the 7,403 write: that pair is the one."""
        newest_first = [_write("7403", "7500", our_price="5525"),
                        _write("5994", "7403", our_price="4473")]
        assert rule.ratio_base(newest_first, D("5525")) == D("7403")

    def test_a_held_or_reverted_action_is_skipped(self):
        newest_first = [_write("9999", "9999", status="held"), _write("5994", "7242")]
        assert rule.ratio_base(newest_first, ON_GOIBIBO) == D("5994")

    def test_a_site_price_unlike_any_write_reads_the_cell(self):
        assert rule.ratio_base([_write("5994", "7242")], D("5100")) is None


class TestTheTwelfthOfNovemberReplayed:
    def test_the_rate_stops_after_one_write_per_target(self):
        history = []
        cell = D("5994")
        # 12:27: target 5,405; then Sterling moved and it is 5,525 from 12:46 on.
        for target in ["5405"] + ["5525"] * 10:
            new = _next_rate(history, target, cell=cell)
            if new != cell:
                history.append(_write(cell, new))
                cell = new
        writes = [h.applied_rms for h in history]
        assert len(writes) == 2, writes
        assert cell < D("7500"), f"climbed to {cell}"

    def test_the_deluxe_row_comes_back_down_from_23066(self):
        """Once fixed, the next run on DELUXE CP writes what the rule wanted."""
        history = [_write("6685", "8081", our_price="4888"), _write("8081", "8245", our_price="4888"),
                   _write("8245", "9967", our_price="4888"), _write("19079", "23066", our_price="4888")]
        new = _next_rate(history, "6029", our_price="4888", cell="23066")
        assert new == rule.to_rms(D("6029"), D("4888"), D("6685"), round_to=10, exact=True, with_tax=True)
        assert new < D("8300")


class TestNotFromOldReadings:
    """Goibibo stopped answering at 12:15; the runs went on until 16:39."""

    from datetime import UTC as _UTC, datetime as _dt
    NOW = _dt(2026, 11, 12, 11, 9, tzinfo=_UTC)  # 16:39 IST

    def test_four_hour_old_prices_are_not_written_from(self):
        from datetime import UTC, datetime
        why = rule.stale_reason({"ASG HOLIDAY RESORTS": datetime(2026, 11, 12, 6, 42, tzinfo=UTC),
                                 "Sterling": datetime(2026, 11, 12, 6, 51, tzinfo=UTC)},
                                self.NOW, "Goibibo")
        assert why and "Goibibo has not been read" in why and "ASG HOLIDAY RESORTS" in why

    def test_one_stale_hotel_is_enough(self):
        from datetime import UTC, datetime
        why = rule.stale_reason({"ASG HOLIDAY RESORTS": datetime(2026, 11, 12, 10, 50, tzinfo=UTC),
                                 "Sterling": datetime(2026, 11, 12, 6, 51, tzinfo=UTC)},
                                self.NOW, "Goibibo")
        assert why and "Sterling" in why and "ASG" not in why

    def test_never_read_tonight(self):
        assert "Sterling never" in rule.stale_reason({"Sterling": None}, self.NOW, "Goibibo")

    def test_fresh_readings_write(self):
        from datetime import UTC, datetime
        assert rule.stale_reason({"ASG HOLIDAY RESORTS": datetime(2026, 11, 12, 10, 40, tzinfo=UTC),
                                  "Sterling": datetime(2026, 11, 12, 9, 30, tzinfo=UTC)},
                                 self.NOW, "Goibibo") is None
