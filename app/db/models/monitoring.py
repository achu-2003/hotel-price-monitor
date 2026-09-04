"""What to monitor, when it last ran, and what went wrong."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
    Integer, Numeric, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.errors import ErrorClass
from app.db.base import Base, TimestampMixin, pg_enum
from app.db.models.enums import CheckRunStatus, CircuitState, DateStrategy

if TYPE_CHECKING:
    from app.db.models.hotel import HotelSource


class MonitorTarget(Base, TimestampMixin):
    """One monitoring instruction: this hotel, on this source, for this stay.

    Note what is NOT here: room type. You configure a stay window; the rooms
    are DISCOVERED from the site. You cannot pre-configure "Deluxe Room" for a
    competitor because the site decides what its rooms are called, and renames
    them. One fetch of a target yields many offers.

    Scheduling lives here rather than in Celery Beat's config so intervals are
    editable from the dashboard without restarting anything.
    """

    __tablename__ = "monitor_targets"
    __table_args__ = (
        # The dispatcher's hot query: "what is due right now?"
        Index("ix_targets_due", "is_enabled", "next_run_at"),
        Index("ix_targets_circuit", "circuit_state"),
        CheckConstraint("interval_minutes >= 5", name="interval_at_least_5_min"),
        CheckConstraint("adults >= 1", name="at_least_one_adult"),
        CheckConstraint(
            "(date_strategy = 'fixed' AND fixed_check_in IS NOT NULL "
            " AND fixed_check_out IS NOT NULL)"
            " OR (date_strategy = 'rolling' AND lead_time_days IS NOT NULL "
            " AND length_of_stay_nights IS NOT NULL)",
            name="date_strategy_fields_present",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_source_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ── booking conditions ───────────────────────────────────────────
    adults: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    children: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rooms: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    meal_plan_filter: Mapped[str | None] = mapped_column(String(60))

    # ── which dates ──────────────────────────────────────────────────
    date_strategy: Mapped[DateStrategy] = mapped_column(
        pg_enum(DateStrategy, "date_strategy"), nullable=False
    )
    fixed_check_in: Mapped[date | None] = mapped_column(Date)
    fixed_check_out: Mapped[date | None] = mapped_column(Date)

    # Rolling windows GENERATE absolute dates at dispatch time; the resolved
    # dates then go into the offer_key. Comparing "7 days out" today against
    # "7 days out" yesterday would compare two DIFFERENT nights and report a
    # change that never happened. See app/services/dates.py.
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    length_of_stay_nights: Mapped[int | None] = mapped_column(Integer)

    # ── scheduling ───────────────────────────────────────────────────
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── alert sensitivity (overrides the global defaults) ─────────────
    min_delta_abs: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_delta_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    confirm_checks: Mapped[int | None] = mapped_column(Integer)

    # ── health ───────────────────────────────────────────────────────
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    circuit_state: Mapped[CircuitState] = mapped_column(
        pg_enum(CircuitState, "circuit_state"),
        default=CircuitState.CLOSED,
        nullable=False,
    )
    circuit_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    hotel_source: Mapped[HotelSource] = relationship(back_populates="monitor_targets")

    def is_stale(self, now: datetime) -> bool:
        """No successful check for 3 intervals.

        Silent failure is the mode that actually costs money: the dashboard
        looks fine, the prices are just frozen. Alerting on silence catches it.

        A target that has NEVER succeeded is measured from when it was created,
        not treated as stale on sight. "Gone quiet" means something stopped;
        a target added a minute ago has not stopped, it has not started — and
        putting it in the alarm list the instant it is created is how people
        learn to ignore the alarm list.

        Once three intervals have passed with no success, it is genuinely wrong
        and does belong there.
        """
        baseline = self.last_success_at or self.created_at
        if baseline is None:
            # No creation timestamp yet: the row is mid-insert, not stale.
            return False
        return (now - baseline).total_seconds() > self.interval_minutes * 60 * 3

    def __repr__(self) -> str:
        return f"<MonitorTarget {self.id} hs={self.hotel_source_id} every {self.interval_minutes}m>"


class CheckRun(Base):
    """One execution of one target. Backs the dashboard's live run status."""

    __tablename__ = "check_runs"
    __table_args__ = (Index("ix_check_runs_target_time", "monitor_target_id", "started_at"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    monitor_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitor_targets.id", ondelete="SET NULL")
    )
    triggered_by: Mapped[str] = mapped_column(String(40), default="scheduler", nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[CheckRunStatus] = mapped_column(
        pg_enum(CheckRunStatus, "check_run_status"), nullable=False
    )

    check_in: Mapped[date | None] = mapped_column(Date)
    check_out: Mapped[date | None] = mapped_column(Date)

    offers_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    offers_unmatched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changes_detected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_summary: Mapped[str | None] = mapped_column(Text)

    # The source said, in so many words, that it had no rooms for the window
    # in check_in/check_out. A SUCCESSFUL run: the page loaded and was read,
    # and what it said was "sold out". Without this the row is indistinguishable
    # from a run that found nothing and could not say why, and the dashboard
    # showed "0 offers, 0 changes" for a hotel that was simply full.
    sold_out: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # One sentence for a person, on a run that needs explaining: which night
    # was full, and which night was priced instead. ``error_summary`` is for
    # runs that failed; this is for runs that succeeded and still have
    # something to say.
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<CheckRun {self.id[:8]} {self.status}>"


class MonitoringError(Base):
    """A failure, classified.

    The class determines the retry policy (see app/core/errors.py). Screenshot
    and HTML paths are what turn "the adapter broke" into a ten-minute fix.
    """

    __tablename__ = "monitoring_errors"
    __table_args__ = (
        Index("ix_errors_unresolved", "resolved_at", "occurred_at"),
        Index("ix_errors_hotel_time", "hotel_id", "occurred_at"),
        Index("ix_errors_class", "error_class"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitor_targets.id", ondelete="SET NULL"), index=True
    )
    hotel_id: Mapped[int | None] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    check_run_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_class: Mapped[ErrorClass] = mapped_column(
        pg_enum(ErrorClass, "error_class"), nullable=False
    )
    is_transient: Mapped[bool] = mapped_column(Boolean, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSONB)  # scrubbed before write

    screenshot_path: Mapped[str | None] = mapped_column(Text)
    html_path: Mapped[str | None] = mapped_column(Text)

    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<MonitoringError {self.error_class} hotel={self.hotel_id}>"


class AlertDefaults(Base, TimestampMixin):
    """How big a move has to be before anybody is told, deployment-wide.

    ONE ROW, ENFORCED
    =================
    There is exactly one deployment-wide default, so the table holds exactly
    one row and the primary key is pinned to 1. A second row would be a second
    answer to a question with one answer, and whichever the query happened to
    return first would quietly become the policy.

    WHY NOT AN ENVIRONMENT VARIABLE
    ===============================
    These started in Settings, and the values there are still the fallback for
    a deployment that has never saved any. But sensitivity is not deployment
    configuration -- it is an operating decision, made by the person watching
    the alerts, usually right after being woken by one. Requiring an edit to
    .env and a restart of five services put that decision behind the one door
    they cannot open.

    BOTH FLOORS, NOT EITHER
    =======================
    A move must clear the rupee amount AND the percentage -- see
    ``comparison.Thresholds``. That is not obvious from a form with two boxes,
    and it matters: 50 rupees on a 1,700 rupee room is 2.9% and alerts, while
    50 rupees on a 17,000 rupee suite is 0.3% and does not. The page says so
    beside the fields, because somebody setting "tell me about 50 rupees" and
    hearing nothing about an expensive room will conclude the system is broken.
    """

    __tablename__ = "alert_defaults"
    __table_args__ = (
        CheckConstraint("id = 1", name="alert_defaults_is_a_singleton"),
        CheckConstraint("min_delta_abs >= 0", name="alert_defaults_abs_not_negative"),
        CheckConstraint(
            "min_delta_pct >= 0 AND min_delta_pct <= 100",
            name="alert_defaults_pct_in_range",
        ),
        CheckConstraint(
            "confirm_checks >= 1 AND confirm_checks <= 10",
            name="alert_defaults_confirm_in_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    min_delta_abs: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    min_delta_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    #: How many consecutive checks must agree before a move is announced. The
    #: debounce that stops a single odd read becoming an alert.
    confirm_checks: Mapped[int] = mapped_column(Integer, nullable=False)

