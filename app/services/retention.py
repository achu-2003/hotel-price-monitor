"""What the database keeps, and for how long.

WHY THIS EXISTS
===============
Everything the monitor learns is written down, and until now almost none of it
was ever thrown away. One hotel producing twenty price changes a day is seven
thousand rows a year; the check runs behind them are heavier still, because a
run is recorded whether or not it found anything to say. Across a fleet the
tables reach six figures inside a year, and none of it is read: the dashboard
asks about this week, the alerts about this hour.

Only the raw observations were swept, by dropping monthly partitions after two
years -- so the one table with a retention policy was also the one that kept
the longest. ``price_changes``, ``check_runs``, ``monitoring_errors`` and
``notifications`` had none at all and grew forever.

WHAT IS DELETED, AND WHAT IS NOT
================================
The rule is history versus setup, and the line is drawn on whether losing the
row changes what the system DOES tomorrow.

Deleted -- an account of something that already happened:

    price_changes       a move that was detected, and announced or suppressed
    check_runs          one execution of one target
    monitoring_errors   a failure, with its screenshot path
    notifications       one message sent to one person
    price_observations  the raw readings, by dropping whole month partitions

Kept, deliberately, and this list is the safety of the whole module:

    hotels, hotel_sources, monitor_targets, room_types, aliases, recipients
                        the setup. Deleting any of it stops the monitoring.
    price_series        NOT history. It carries ``last_price`` -- the baseline
                        every comparison is made against. Delete it and the
                        next check has nothing to compare to, so either every
                        hotel reports a change from nothing or the series
                        silently re-baselines at whatever today's price is.
    audit_log           who did what, including who ran this sweep. It is the
                        record that survives the thing it describes, which is
                        the entire point of having it, and it is tiny.
    unmatched_offers    open work waiting for a person, not a past event.

A NOTE ON THE CARRY-OVER GUARD
==============================
``ingest`` checks ``price_changes`` for an already-recorded carry-over before
writing one, with no time bound. That guard only matters when a price_series
row is deleted and rebuilt -- and this sweep never touches price_series, so
ageing out the change it would have found cannot make it fire twice.

NOTHING HERE RUNS ON A SCHEDULE
===============================
There was a monthly Celery task and it was taken out again. A job that quietly
deletes months of a business's own history, on a calendar, unattended, is not
worth the disk it saves: the one time it matters is the time somebody wanted
last quarter back. So the clean is a person on the settings page, looking at
the counts, pressing a button -- and the audit row records which person.

The consequence to be honest about: these tables are now bounded only by
somebody remembering. Nothing will tell them.

STATEMENTS, NOT EXECUTION
=========================
The module hands back statements rather than running them, so counting and
deleting cannot drift into two different ideas of "old" -- the settings page
measures with the same predicate the button deletes with. It is also what
would let a scheduled caller hold a sync session if one is ever wanted again.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Delete, Select, delete, func, select, text

from app.db.models import CheckRun, MonitoringError, Notification, PriceChange
from app.db.models.price import PriceObservation

#: Months of history kept by default. One, because that is what this is for:
#: the dashboard asks about this week and the alerts about this hour, and a
#: year of confirmed price moves is a year of rows nobody opens.
#:
#: The cost of setting it low is real and worth stating plainly -- "what did
#: this competitor charge last Diwali" stops having an answer once the month
#: containing it is gone. Raise it if that question matters more than the disk.
DEFAULT_KEEP_MONTHS = 1


@dataclass(frozen=True, slots=True)
class Sweep:
    """One table's worth of the policy: what to count, and what to delete."""

    key: str
    #: What a person reading the settings page should understand is going.
    label: str
    #: Rows past the window -- what a clean would delete.
    count: Select
    #: Rows altogether, so the number above is shown with a denominator. "412
    #: of 9,318" is a decision; "412" on its own is a number.
    total: Select
    purge: Delete


def add_months(anchor: date, months: int) -> date:
    """Month arithmetic on the first of a month.

    ``timedelta(days=30)`` drifts by a day or two per month and would
    eventually create a partition boundary that does not line up with a month,
    which Postgres rejects with an overlap error.
    """
    month_index = anchor.year * 12 + (anchor.month - 1) + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def month_from_name(name: str) -> date | None:
    """``price_observations_2026_08`` -> 2026-08-01."""
    parts = name.rsplit("_", 2)
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[1]), int(parts[2]), 1)
    except ValueError:
        return None


def cutoff_for(now: datetime, keep_months: int = DEFAULT_KEEP_MONTHS) -> datetime:
    """The moment before which history is discarded.

    Counted back from ``now`` rather than from the first of the month, so a
    sweep run on the 2nd does not delete almost two months on a one-month
    policy. Whatever the calendar says, the window is always the promised
    length.

    ``keep_months`` below 1 is refused. A zero window deletes the run that is
    happening right now, including the errors a person is in the middle of
    reading, and no configuration mistake should be able to ask for that.
    """
    if keep_months < 1:
        raise ValueError(f"keep_months must be at least 1, got {keep_months}")
    first = add_months(now.date().replace(day=1), -keep_months)
    # The 31st has no counterpart in a 30-day month. Clamping to that month's
    # last day keeps the window at most the promised length; rolling forward
    # into the next month would silently delete a day nobody agreed to.
    last_day = monthrange(first.year, first.month)[1]
    return datetime.combine(first.replace(day=min(now.day, last_day)), now.timetz())


def sweeps(cutoff: datetime) -> list[Sweep]:
    """The row tables, in the order they are swept.

    Order matters only for readability -- none of these carries a foreign key
    to another, which is checked by a test, so no delete here can orphan a row
    or fail on a constraint.
    """
    return [
        Sweep(
            key="price_changes",
            label="price changes",
            count=select(func.count(PriceChange.id)).where(PriceChange.changed_at < cutoff),
            total=select(func.count(PriceChange.id)),
            purge=delete(PriceChange).where(PriceChange.changed_at < cutoff),
        ),
        Sweep(
            key="check_runs",
            label="check runs",
            count=select(func.count(CheckRun.id)).where(CheckRun.started_at < cutoff),
            total=select(func.count(CheckRun.id)),
            purge=delete(CheckRun).where(CheckRun.started_at < cutoff),
        ),
        Sweep(
            key="monitoring_errors",
            label="recorded errors",
            count=select(func.count(MonitoringError.id)).where(
                MonitoringError.occurred_at < cutoff
            ),
            total=select(func.count(MonitoringError.id)),
            purge=delete(MonitoringError).where(MonitoringError.occurred_at < cutoff),
        ),
        Sweep(
            key="notifications",
            label="sent messages",
            count=select(func.count(Notification.id)).where(Notification.created_at < cutoff),
            total=select(func.count(Notification.id)),
            purge=delete(Notification).where(Notification.created_at < cutoff),
        ),
    ]


def observations_count(cutoff: datetime) -> Select:
    """How many raw readings are older than the window.

    Reported separately from the partitions because the two answer different
    questions: this is how much is EXPIRED, the partition list is how much can
    actually be reclaimed this run. See :func:`partitions_to_drop`.
    """
    return select(func.count(PriceObservation.id)).where(PriceObservation.checked_at < cutoff)


#: Every partition currently attached to ``price_observations``.
PARTITION_LIST_SQL = text(
    "SELECT c.relname FROM pg_class c "
    "JOIN pg_inherits i ON i.inhrelid = c.oid "
    "JOIN pg_class p ON p.oid = i.inhparent "
    "WHERE p.relname = 'price_observations'"
)


def partitions_to_drop(names: list[str], cutoff: datetime) -> list[str]:
    """Which observation partitions are entirely older than the window.

    ENTIRELY, which is why this keeps more than the cutoff asks for. A
    partition holds a whole calendar month, and ``DROP TABLE`` cannot take half
    of one -- so on 5 September with a one-month window, August is still
    holding readings from the 5th onwards and stays until October. Up to a
    month of extra history survives each sweep, and that is the right trade:
    the alternative is a row-by-row ``DELETE`` that takes minutes, locks the
    table the collectors are writing to, and leaves the space to a vacuum that
    may never come. Dropping the partition is instant and gives the disk back
    at once.

    A name this cannot parse is left alone. An unrecognised table under
    ``price_observations`` is something nobody here put there, and dropping it
    on a guess is not a repair.
    """
    doomed = []
    for name in names:
        month = month_from_name(name)
        if month is not None and add_months(month, 1) <= cutoff.date():
            doomed.append(name)
    return doomed


@dataclass(frozen=True, slots=True)
class TableUsage:
    """One table, measured."""

    key: str
    label: str
    expired: int
    total: int


@dataclass(frozen=True, slots=True)
class Usage:
    """What a clean would delete, measured but not deleted."""

    keep_months: int
    cutoff: datetime
    tables: list[TableUsage]
    #: Raw readings past the window, and how many whole monthly partitions of
    #: them can be reclaimed right now. The two differ on purpose -- see
    #: :func:`partitions_to_drop`.
    observations_expired: int
    partitions_droppable: int

    @property
    def total_expired(self) -> int:
        return sum(t.expired for t in self.tables) + self.observations_expired

    @property
    def total_rows(self) -> int:
        return sum(t.total for t in self.tables)


async def measure(session, keep_months: int, *, now: datetime | None = None) -> Usage:
    """Count what is past the window, without deleting any of it.

    Both async callers share this -- the settings page, which renders the
    numbers, and the API the button reads before it asks. A page that showed
    one figure and a confirmation that quoted another would be worse than
    showing neither.
    """
    cutoff = cutoff_for(now or datetime.now(UTC), keep_months)
    return Usage(
        keep_months=keep_months,
        cutoff=cutoff,
        tables=[
            TableUsage(
                key=sweep.key,
                label=sweep.label,
                expired=await session.scalar(sweep.count) or 0,
                total=await session.scalar(sweep.total) or 0,
            )
            for sweep in sweeps(cutoff)
        ],
        observations_expired=await session.scalar(observations_count(cutoff)) or 0,
        partitions_droppable=len(
            partitions_to_drop(list((await session.scalars(PARTITION_LIST_SQL)).all()), cutoff)
        ),
    )
