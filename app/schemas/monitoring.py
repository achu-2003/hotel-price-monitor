"""Monitor targets, check runs, and errors."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from app.core.errors import ErrorClass
from app.db.models.enums import CheckRunStatus, CircuitState, DateStrategy
from app.schemas.common import ORMModel


class MonitorTargetBase(ORMModel):
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    rooms: int = Field(default=1, ge=1, le=10)
    meal_plan_filter: str | None = Field(default=None, max_length=60)

    date_strategy: DateStrategy
    fixed_check_in: date | None = None
    fixed_check_out: date | None = None
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    length_of_stay_nights: int | None = Field(default=None, ge=1, le=30)

    interval_minutes: int = Field(
        default=30,
        ge=5,
        le=1440,
        description="Floor of 5 minutes is a politeness limit, not a technical one.",
    )
    min_delta_abs: Decimal | None = Field(default=None, ge=0)
    min_delta_pct: Decimal | None = Field(default=None, ge=0, le=100)
    confirm_checks: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="How many consecutive checks a new price must survive before "
                    "it counts. 1 disables debouncing and will produce noise.",
    )

    @model_validator(mode="after")
    def _strategy_fields_present(self):
        """Mirror the database check constraint.

        Duplicated on purpose: the constraint is the guarantee, but a 422 with
        a readable message beats a 500 carrying a Postgres error string.
        """
        if self.date_strategy == DateStrategy.FIXED:
            if self.fixed_check_in is None or self.fixed_check_out is None:
                raise ValueError(
                    "A fixed strategy needs both fixed_check_in and fixed_check_out."
                )
            if self.fixed_check_out <= self.fixed_check_in:
                raise ValueError("fixed_check_out must be after fixed_check_in.")
        else:
            if self.lead_time_days is None or self.length_of_stay_nights is None:
                raise ValueError(
                    "A rolling strategy needs both lead_time_days and "
                    "length_of_stay_nights. The absolute dates are generated at "
                    "dispatch time — see app/services/dates.py for why."
                )
        return self


class MonitorTargetCreate(MonitorTargetBase):
    hotel_source_id: int


class MonitorTargetUpdate(ORMModel):
    """Every field optional: PATCH semantics.

    Changing ``interval_minutes`` takes effect at the next dispatch sweep with
    no restart, which is the whole reason scheduling lives in the database
    rather than in Celery Beat's configuration.
    """

    adults: int | None = Field(default=None, ge=1, le=20)
    children: int | None = Field(default=None, ge=0, le=20)
    rooms: int | None = Field(default=None, ge=1, le=10)
    meal_plan_filter: str | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    is_enabled: bool | None = None
    min_delta_abs: Decimal | None = Field(default=None, ge=0)
    min_delta_pct: Decimal | None = Field(default=None, ge=0, le=100)
    confirm_checks: int | None = Field(default=None, ge=1, le=10)
    fixed_check_in: date | None = None
    fixed_check_out: date | None = None
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    length_of_stay_nights: int | None = Field(default=None, ge=1, le=30)
    #: Setting this to ``closed`` is how an operator resumes a paused target
    #: after fixing whatever broke it.
    circuit_state: CircuitState | None = None


class MonitorTargetOut(MonitorTargetBase):
    id: int
    hotel_source_id: int
    hotel_id: int | None = None
    hotel_name: str | None = None
    is_enabled: bool
    next_run_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    circuit_state: CircuitState
    circuit_opened_at: datetime | None
    resolved_check_in: date | None = Field(
        default=None,
        description="The absolute dates this target resolves to today. A "
                    "rolling window means different nights on different days.",
    )
    resolved_check_out: date | None = None


class CheckRunOut(ORMModel):
    id: str
    monitor_target_id: int | None
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None
    status: CheckRunStatus
    check_in: date | None
    check_out: date | None
    offers_found: int
    offers_unmatched: int
    changes_detected: int
    duration_ms: int | None
    error_summary: str | None
    #: The source announced no rooms for check_in/check_out. Defaulted rather
    #: than required so a client reading an older row does not break.
    sold_out: bool = False
    #: Why a successful run still needs explaining — which night was full, and
    #: which night was priced instead.
    notes: str | None = None


class MonitoringErrorOut(ORMModel):
    id: int
    monitor_target_id: int | None
    hotel_id: int | None
    hotel_name: str | None = None
    source_id: int | None
    check_run_id: str | None
    occurred_at: datetime
    error_class: ErrorClass
    is_transient: bool
    message: str
    context: dict[str, Any] | None
    has_screenshot: bool = False
    has_html: bool = False
    retry_count: int
    resolved_at: datetime | None


class ManualOfferIn(ORMModel):
    """One hand-entered price.

    Goes through the identical offer_key / comparison / notification path as a
    scraped price, so a manually tracked hotel behaves exactly like an
    automated one — including its debounce and its alerts.
    """

    room_type_id: int
    check_in: date
    check_out: date
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    price_inclusive: Decimal | None = Field(default=None, ge=0)
    price_exclusive: Decimal | None = Field(default=None, ge=0)
    taxes_fees: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    meal_plan: str | None = None
    refundable: bool | None = None
    is_available: bool = True

    @model_validator(mode="after")
    def _needs_a_price_when_available(self):
        if self.is_available and self.price_inclusive is None and self.price_exclusive is None:
            raise ValueError(
                "An available room needs a price. To record a sold-out room, "
                "set is_available=false instead of entering 0 — they are "
                "different events and must not be conflated."
            )
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in.")
        return self


class ManualEntryIn(ORMModel):
    hotel_source_id: int
    offers: list[ManualOfferIn] = Field(min_length=1, max_length=200)


class AlertDefaultsIn(ORMModel):
    """The deployment-wide alert sensitivity.

    The bounds are the same ones ``MonitorTargetUpdate`` applies to a per-hotel
    override, so a value acceptable in one place is acceptable in the other.

    ``min_delta_abs`` of 0 is allowed and means "no rupee floor" -- the
    percentage alone then decides. That is a legitimate setting for a portfolio
    of similarly-priced rooms, and refusing it would be second-guessing.
    """

    min_delta_abs: Decimal = Field(ge=0, le=1_000_000)
    min_delta_pct: Decimal = Field(ge=0, le=100)
    confirm_checks: int = Field(ge=1, le=10)
    #: Display only -- it never reaches the comparison engine. Defaulted rather
    #: than required so a client that predates it (or the sensitivity form,
    #: which sends three fields) does not silently switch the whole deployment
    #: back to pre-tax prices on every save.
    show_prices_with_tax: bool = False


class AlertDefaultsOut(AlertDefaultsIn):
    """What is stored, plus what it would mean for a room at either extreme.

    A form with two boxes does not convey that BOTH have to be cleared, and
    that is the part people get wrong: 50 rupees on a 1,700 rupee room alerts,
    50 rupees on a 17,000 rupee suite does not. The page shows the arithmetic
    rather than explaining the rule.
    """

    cheapest_room: Decimal | None = None
    dearest_room: Decimal | None = None

