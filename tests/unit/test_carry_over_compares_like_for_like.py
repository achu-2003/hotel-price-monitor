"""A week-ahead rate and a same-day rate are not each other's baseline.

THE INCIDENT THIS FILE IS ABOUT
===============================
Roys Kozee Kaves had been watched at lead 0 for weeks -- tonight's price, every
half hour. The day a lead-7 target was added, its first sighting of 1 Sep went
looking for a baseline, found 25 Aug (exactly seven days back, inside the gap
window, genuinely the most recent night priced) and published five rows:

    Natures Delight   2026-09-01 -> 2026-09-02   vs last night
                      was 2,047.50   now 2,177.50   +130 (6.3%)   sent

Every part of that is wrong except the arithmetic. 25 Aug was not "last night"
relative to 1 Sep. The hotel had not changed its price. What the row actually
reported is that a night a week out costs 130 rupees more than tonight, which
is true, is not news, and arrived as five alerts in somebody's inbox.

THE RULE
========
The carry-over comparison exists for windows that see each night exactly once:
at lead 0 every morning is a brand-new key and nothing would ever be compared.
Its baseline must therefore be the same KIND of reading -- the same distance
ahead of the observation -- or it is comparing two different products.

Lead distance is recovered from the data rather than threaded through the
payload: the night, minus the local date it was first priced on. A series first
seen before lead windows existed still reports the distance it was watched at.

WHAT MUST NOT BREAK
===================
The gap window exists so a monitor that was off over a weekend still finds its
last real baseline. Both nights there were watched on the day -- distance 0 --
so that case is untouched, and it has its own test below as well as in
test_carry_over.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D

from app.services.comparison import (
    CarryOver,
    Observation,
    Thresholds,
    compare_across_stay_dates,
)

DEFAULT = Thresholds(min_delta_abs=D("50"), min_delta_pct=D("2"))
TONIGHT = date(2026, 8, 25)
A_WEEK_OUT = date(2026, 9, 1)


def baseline(price: str, check_in: date) -> CarryOver:
    return CarryOver(
        last_price=D(price), is_available=True, check_in=check_in, offer_key="prev"
    )


def compare(previous: CarryOver, price: str, *, this_check_in: date,
            previous_lead: int, this_lead: int):
    return compare_across_stay_dates(
        previous,
        Observation(price=D(price)),
        DEFAULT,
        this_check_in=this_check_in,
        previous_lead_days=previous_lead,
        this_lead_days=this_lead,
    )


class TestTheIncident:
    """The five rows that should never have been written."""

    def test_a_week_ahead_night_is_not_compared_to_tonight(self):
        assert compare(
            baseline("2047.50", TONIGHT), "2177.50",
            this_check_in=A_WEEK_OUT, previous_lead=0, this_lead=7,
        ) is None

    def test_the_same_numbers_do_report_when_both_are_week_ahead(self):
        """Not a blanket silencing of the window. Once the lead-7 target has a
        yesterday of its own, its comparisons are real and must arrive."""
        result = compare(
            baseline("2047.50", date(2026, 8, 31)), "2177.50",
            this_check_in=A_WEEK_OUT, previous_lead=7, this_lead=7,
        )

        assert result is not None
        assert result.delta == D("130")


class TestWhatMustKeepWorking:
    def test_consecutive_nights_at_lead_zero_still_report(self):
        """The case this function was written for."""
        result = compare(
            baseline("2000", date(2026, 8, 24)), "2200",
            this_check_in=TONIGHT, previous_lead=0, this_lead=0,
        )

        assert result is not None and result.delta == D("200")

    def test_a_weekend_outage_still_reports(self):
        """Both nights were watched on the day, so both are distance 0. The
        gap window is what covers the missing days, and it still does."""
        result = compare(
            baseline("2000", date(2026, 8, 22)), "2200",
            this_check_in=TONIGHT, previous_lead=0, this_lead=0,
        )

        assert result is not None
        assert result.previous_check_in == date(2026, 8, 22)

    def test_an_unknown_distance_does_not_suppress_the_comparison(self):
        """Series predating this change carry no distance. Silence would be a
        worse answer than the old behaviour, so absence means "do not judge"."""
        result = compare_across_stay_dates(
            baseline("2000", date(2026, 8, 24)),
            Observation(price=D("2200")),
            DEFAULT,
            this_check_in=TONIGHT,
        )

        assert result is not None


class TestLeadDistanceIsDerivedNotDeclared:
    """The distance comes from the night and the day it was first priced, so a
    target's lead time is recovered without the payload carrying it."""

    def test_a_night_priced_on_the_day_is_distance_zero(self):
        from app.services.ingest import _lead_days

        assert _lead_days(TONIGHT, datetime(2026, 8, 25, 9, 0, tzinfo=UTC)) == 0

    def test_a_night_priced_a_week_early_is_distance_seven(self):
        from app.services.ingest import _lead_days

        assert _lead_days(A_WEEK_OUT, datetime(2026, 8, 25, 9, 0, tzinfo=UTC)) == 7

    def test_the_local_date_decides_not_the_utc_one(self):
        """19:00 UTC is already tomorrow in Asia/Kolkata. Using the UTC date
        would put a third of every day's readings in the wrong window."""
        from app.services.ingest import _lead_days

        assert _lead_days(date(2026, 8, 26), datetime(2026, 8, 25, 19, 0, tzinfo=UTC)) == 0
