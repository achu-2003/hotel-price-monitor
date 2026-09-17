"""Shapes for the repricing rule, the room mapping, and a run's progress."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.schemas.common import ORMModel


class RepricingSettingsIn(ORMModel):
    auto_enabled: bool = False
    position_pct: Decimal = Field(default=Decimal("0"), ge=-50, le=50)
    max_step_pct: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    floor_pct: Decimal = Field(default=Decimal("30"), ge=0, le=90)
    ceiling_pct: Decimal = Field(default=Decimal("50"), ge=0, le=500)
    min_competitors: int = Field(default=2, ge=1, le=50)
    round_to: int = Field(default=10, ge=1, le=1000)
    channel: str = Field(default="Booking.com", min_length=1, max_length=120)

    @field_validator("channel")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class RepricingSettingsOut(RepricingSettingsIn):
    updated_at: datetime | None = None


class MappingIn(ORMModel):
    """Which RMS row one of the owner's rooms is. Blank rate types mean "not sold"."""

    rms_room: str = Field(min_length=1, max_length=200)
    rate_type_ep: str | None = Field(default=None, max_length=200)
    rate_type_cp: str | None = Field(default=None, max_length=200)
    rate_type_map: str | None = Field(default=None, max_length=200)
    floor_amount: Decimal | None = Field(default=None, ge=0)
    ceiling_amount: Decimal | None = Field(default=None, ge=0)
    is_enabled: bool = True

    @field_validator("rms_room", "rate_type_ep", "rate_type_cp", "rate_type_map")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        return value or None


class MappingOut(MappingIn):
    room_type_id: int
    room_name: str


class RunIn(ORMModel):
    mode: str = Field(default="manual", pattern="^(manual|dry_run)$")


class RunStatus(ORMModel):
    status: str
    ok: bool | None = None
    message: str | None = None
    applied: int | None = None
    failed: int | None = None
    check_in: str | None = None
    has_screenshot: bool = False
    updated_at: datetime | None = None
