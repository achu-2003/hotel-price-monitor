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
    weekend_pct: Decimal = Field(default=Decimal("0"), ge=0, le=50)
    sold_out_pct: Decimal = Field(default=Decimal("10"), ge=0, le=50)

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
    #: Guest prices the owner typed over the rule's, by room type id. Rooms
    #: left out keep the rule's proposal.
    overrides: dict[int, Decimal] = Field(default_factory=dict, max_length=100)
    #: Do this ONE room and leave every other rate untouched. ``None`` is the
    #: whole property, which is what the two buttons under the table send.
    #:
    #: One login either way -- the browser cost is the same -- so this is not
    #: an optimisation. It is for the night when one room's proposal is right
    #: and the rest are not, which used to mean clearing three boxes back to
    #: the rule's number and hoping none were missed.
    only_room_type_id: int | None = Field(default=None, gt=0)

    #: Other RMS channels the owner ticked, per room: ``{room_type_id:
    #: {channel: rms_rate}}``. The rate is the RMS figure for the room's
    #: anchor plan on that channel, as the page showed it; the channel's
    #: other plans keep their supplement over it. Nothing here means only the
    #: main channel is written.
    channels: dict[int, dict[str, Decimal]] = Field(default_factory=dict, max_length=100)
    #: Rooms whose MAIN channel (Booking.com) the owner unticked: read, not written.
    skip_primary: list[int] = Field(default_factory=list, max_length=100)

    @field_validator("overrides")
    @classmethod
    def _positive(cls, value: dict[int, Decimal]) -> dict[int, Decimal]:
        for room_type_id, price in value.items():
            if not (0 < price < 10_000_000):
                raise ValueError(f"the price for room {room_type_id} must be a positive amount")
        return value

    @field_validator("channels")
    @classmethod
    def _channel_rates(cls, value: dict[int, dict[str, Decimal]]) -> dict[int, dict[str, Decimal]]:
        for room_type_id, picks in value.items():
            if len(picks) > 20:
                raise ValueError(f"too many channels for room {room_type_id}")
            for channel, rate in picks.items():
                if not channel.strip() or len(channel) > 120:
                    raise ValueError(f"room {room_type_id}: a channel name is missing or too long")
                if not (0 < rate < 10_000_000):
                    raise ValueError(f"room {room_type_id}: the {channel} rate must be a positive amount")
        return value


class RunStatus(ORMModel):
    status: str
    ok: bool | None = None
    message: str | None = None
    applied: int | None = None
    failed: int | None = None
    check_in: str | None = None
    has_screenshot: bool = False
    updated_at: datetime | None = None
