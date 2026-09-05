"""What the monthly clean deletes, and -- mostly -- what it must not.

WHY THIS FILE IS MOSTLY ABOUT WHAT SURVIVES
===========================================
Everything the monitor learns was written down and almost none of it was ever
thrown away. Twenty price changes a day on one hotel is seven thousand rows a
year; the check runs behind them are heavier, because a run is recorded whether
or not it found anything to say. Only the raw observations had a retention
policy at all -- and that one kept two years, so the single table being swept
was also the one keeping longest.

So a sweep was added. The risk it introduces is not that it deletes too little.

``price_series`` is the one that would be quietest and worst. It looks like
history -- a row per offer, with a price on it -- and it is not: it carries
``last_price``, the baseline every comparison is made against. Sweeping it
would leave every hotel with nothing to compare tomorrow's price to, so either
every room reports a change from nothing or the series silently re-baselines at
whatever today's number happens to be, and no alert would say either had
happened. Right behind it are the hotels, sources, selectors, targets and
recipients: delete those and monitoring does not degrade, it stops.

That is why most of what follows pins the exclusions rather than the deletions.
A sweep that misses a table wastes disk. A sweep that takes the wrong one is
undetectable from the screens.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import retention


class TestTheWindow:
    """``cutoff_for`` is the single definition of "old". Both callers ask it."""

    def test_one_month_back_keeps_the_day(self):
        """Counted from now, not from the first of the month. Anchoring to the
        1st would delete almost two months on a one-month policy whenever the
        sweep happened to run late in the month."""
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, 8, 30, tzinfo=UTC), 1)

        assert cutoff == datetime(2026, 8, 5, 8, 30, tzinfo=UTC)

    def test_a_longer_window_reaches_further_back(self):
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, 8, 30, tzinfo=UTC), 6)

        assert cutoff == datetime(2026, 3, 5, 8, 30, tzinfo=UTC)

    def test_it_crosses_a_year_boundary(self):
        cutoff = retention.cutoff_for(datetime(2026, 1, 15, 0, 0, tzinfo=UTC), 2)

        assert cutoff == datetime(2025, 11, 15, 0, 0, tzinfo=UTC)

    def test_the_31st_clamps_rather_than_rolling_forward(self):
        """February has no 31st. Rolling into March would put the cutoff LATER
        than the window promises and silently delete days nobody agreed to."""
        cutoff = retention.cutoff_for(datetime(2026, 3, 31, 9, 0, tzinfo=UTC), 1)

        assert cutoff == datetime(2026, 2, 28, 9, 0, tzinfo=UTC)

    def test_the_timezone_survives(self):
        """A naive cutoff compared against timezone-aware columns raises in
        Postgres, and the comparison is the whole statement."""
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, 8, 30, tzinfo=UTC), 1)

        assert cutoff.tzinfo is not None

    @pytest.mark.parametrize("months", [0, -1, -12])
    def test_a_window_of_nothing_is_refused(self, months):
        """A zero window deletes the run happening right now, including the
        errors somebody is in the middle of reading. No configuration mistake
        should be able to ask for that."""
        with pytest.raises(ValueError):
            retention.cutoff_for(datetime(2026, 9, 5, tzinfo=UTC), months)


class TestWhatIsSwept:
    SWEPT = {"price_changes", "check_runs", "monitoring_errors", "notifications"}

    def test_the_tables_that_grow_are_all_covered(self):
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, tzinfo=UTC), 1)

        assert {s.key for s in retention.sweeps(cutoff)} == self.SWEPT

    def test_the_setup_is_never_swept(self):
        """The list this sweep must not touch. Deleting any of these does not
        degrade monitoring, it stops it -- and price_series is the one that
        would fail quietly, because it looks like history and is actually the
        baseline every comparison is made against."""
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, tzinfo=UTC), 1)
        swept = " ".join(str(s.purge) for s in retention.sweeps(cutoff))

        for table in (
            "hotels", "hotel_sources", "sources", "monitor_targets",
            "room_types", "room_type_aliases", "recipients", "hotel_recipients",
            "price_series", "audit_log", "unmatched_offers", "users",
        ):
            assert f"FROM {table}" not in swept
            assert f"DELETE FROM {table}" not in swept

    def test_counting_and_deleting_ask_the_same_question(self):
        """The page shows a number and the button deletes rows. If the two
        predicates could drift, the confirmation would quote a figure that had
        nothing to do with what went."""
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, tzinfo=UTC), 1)

        for sweep in retention.sweeps(cutoff):
            assert str(sweep.count.whereclause) == str(sweep.purge.whereclause)

    def test_every_sweep_is_bounded_by_the_cutoff(self):
        """A purge with no WHERE empties the table. There is no version of
        this feature where that is correct, so it is checked rather than
        assumed."""
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, tzinfo=UTC), 1)

        for sweep in retention.sweeps(cutoff):
            assert sweep.purge.whereclause is not None
            assert sweep.count.whereclause is not None

    def test_each_table_is_labelled_for_a_person(self):
        """These strings are what the settings page shows next to a count. A
        table name is not an explanation of what is about to be deleted."""
        cutoff = retention.cutoff_for(datetime(2026, 9, 5, tzinfo=UTC), 1)

        for sweep in retention.sweeps(cutoff):
            assert sweep.label and sweep.label != sweep.key


class TestObservationPartitions:
    """Raw readings are dropped a whole month at a time, or not at all."""

    #: Derived rather than written down, so this reads the boundary the sweep
    #: will actually use. A hand-written cutoff here would test the partition
    #: rule against a date the window never produces.
    CUTOFF = retention.cutoff_for(datetime(2026, 9, 5, 8, 30, tzinfo=UTC), 1)
    PARTITIONS = [
        "price_observations_2026_06",
        "price_observations_2026_07",
        "price_observations_2026_08",
        "price_observations_2026_09",
    ]

    def test_months_entirely_past_the_window_go(self):
        dropped = retention.partitions_to_drop(self.PARTITIONS, self.CUTOFF)

        assert "price_observations_2026_06" in dropped
        assert "price_observations_2026_07" in dropped

    def test_the_fixture_puts_the_cutoff_inside_a_partition(self):
        """The case the next test is about. If the window ever lands on the
        1st this file stops testing the interesting half, and would keep
        passing while it did."""
        assert self.CUTOFF.day != 1

    def test_the_month_the_cutoff_falls_inside_stays(self):
        """On 5 September a one-month window cuts at 5 August, and August's
        partition still holds everything from the 5th onwards. DROP TABLE
        cannot take half a partition, so the whole month waits for October --
        keeping up to a month more than asked, which is the price of not
        running a row-by-row DELETE against the table the collectors are
        writing to."""
        dropped = retention.partitions_to_drop(self.PARTITIONS, self.CUTOFF)

        assert "price_observations_2026_08" not in dropped

    def test_the_current_month_stays(self):
        dropped = retention.partitions_to_drop(self.PARTITIONS, self.CUTOFF)

        assert "price_observations_2026_09" not in dropped

    def test_a_name_it_cannot_parse_is_left_alone(self):
        """Something nobody here put under price_observations. Dropping it on
        a guess is not a repair."""
        dropped = retention.partitions_to_drop(
            ["price_observations", "price_observations_backup", "something_else"],
            self.CUTOFF,
        )

        assert dropped == []

    def test_nothing_to_drop_is_not_an_error(self):
        assert retention.partitions_to_drop([], self.CUTOFF) == []


class TestTheDefaults:
    def test_the_deployment_keeps_one_month(self):
        from app.config import Settings

        assert Settings().history_retention_months == 1
        assert retention.DEFAULT_KEEP_MONTHS == 1

    def test_nothing_deletes_history_on_a_schedule(self):
        """Deliberate, and the opposite of what this shipped with first.

        A job that quietly deletes months of a business's own history on a
        calendar, unattended, is not worth the disk it saves -- the one time it
        matters is the time somebody wanted last quarter back. The clean is a
        person pressing a button, and the audit row says which person.

        Asserted rather than left to the absence of code, because a schedule is
        one dict entry: it would come back as a plausible-looking three lines
        in a file that already has seven of them, and nothing else on the way
        to production would notice.
        """
        from app.workers.celery_app import celery_app

        assert "clean-history" not in celery_app.conf.beat_schedule
        tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
        assert "maintenance.clean_history" not in tasks

    def test_the_partition_drop_is_still_scheduled(self):
        """The one sweep that DOES run on its own, and stays that way. It drops
        observation partitions past two years -- a different window, a different
        table, and old enough that nobody is holding their breath for it."""
        from app.workers.celery_app import celery_app

        assert celery_app.conf.beat_schedule["retention-sweep"]["task"] == (
            "maintenance.retention_sweep"
        )

    def test_the_partition_sweep_and_this_one_agree_about_months(self):
        """Two sweeps deciding where a month begins by two definitions is how
        a boundary drifts by a day and a partition gets dropped early."""
        from app.workers import tasks_maintenance

        assert tasks_maintenance._add_months is retention.add_months
        assert tasks_maintenance._month_from_name is retention.month_from_name
