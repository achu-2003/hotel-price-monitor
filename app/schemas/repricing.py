"""Shapes for the repricing rule, the room mapping, and a run's progress."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.schemas.common import ORMModel


class AutomaticIn(ORMModel):
    """What the toggle beside Apply sends, and nothing else.

    Its own shape rather than a corner of the rule's, so that the request
    which lets rates move by themselves cannot arrive as a side effect of
    saving a percentage.
    """

    enabled: bool


class RepricingSettingsIn(ORMModel):
    #: OMITTED MEANS "LEAVE IT ALONE", and that is not a convenience.
    #:
    #: The switch lives beside Apply now, not in this form, so the form no
    #: longer sends it -- and a plain ``bool = False`` would then have Save
    #: rule turn automation OFF every time somebody adjusted a percentage,
    #: silently, with the page still showing the toggle on. The switch has
    #: its own endpoint (``PUT /automatic``) so that turning rates loose is
    #: always a deliberate act with its own audit line.
    auto_enabled: bool | None = None
    position_pct: Decimal = Field(default=Decimal("0"), ge=-50, le=50)
    max_step_pct: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    floor_pct: Decimal = Field(default=Decimal("30"), ge=0, le=90)
    ceiling_pct: Decimal = Field(default=Decimal("50"), ge=0, le=500)
    min_competitors: int = Field(default=2, ge=1, le=50)
    round_to: int = Field(default=10, ge=1, le=1000)
    channel: str = Field(default="Booking.com", min_length=1, max_length=120)
    weekend_pct: Decimal = Field(default=Decimal("0"), ge=0, le=50)
    sold_out_pct: Decimal = Field(default=Decimal("10"), ge=0, le=50)
    #: The one competitor this owner prices against, or None for the median
    #: rule. Checked against the caller's own active competitors in the
    #: route -- a bare id here would let one account name another's hotel.
    benchmark_hotel_id: int | None = Field(default=None, gt=0)
    #: Rupees under the benchmark. 0 is "match them", which is a real choice;
    #: the upper bound is a typo guard, not a policy.
    benchmark_undercut: Decimal = Field(default=Decimal("100"), ge=0, le=100000)
    #: Measure the gap on what the guest pays, tax included. See the model.
    benchmark_with_tax: bool = True
    #: One of services.meal_plan's plans, or None for "whatever is cheapest".
    benchmark_meal_plan: str | None = Field(default=None, max_length=60)
    #: ``{our room_type_id: their room name}``. Every key is checked against
    #: the caller's own property in the route.
    benchmark_room_pairs: dict[int, str] = Field(default_factory=dict, max_length=50)

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
    #: Extra channels a Preview should read, by name. Empty is the default and
    #: means the rule's channel alone.
    #:
    #: It used to read every channel the grid listed, for every mapped room,
    #: on every Preview: ninety cells, each its own click and settle, twenty
    #: minutes. Apply touches one channel, so a Preview that answers "what is
    #: Apply about to do" needs one channel -- and the owner who genuinely
    #: wants to see Goibibo can ask for Goibibo.
    preview_channels: list[str] = Field(default_factory=list, max_length=20)

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
