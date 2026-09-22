"""Repricing: the owner's rule, the room mapping, and the record of every move.

See ``app/services/repricing.py`` for what the rule does with these rows and
``app/services/rate_app_rms.py`` for how a decided rate reaches the grid.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.services.meal_plan import rms_plan

#: The meal plans a rate can be set for, in the order RMS lists them. EP is
#: room only, CP adds breakfast, MAP adds breakfast and one main meal.
PLANS = ("EP", "CP", "MAP")


class RepricingSettings(Base, TimestampMixin):
    """How far, and whether, the owner's rate follows the market.

    ONE ROW PER OWNER, OFF BY DEFAULT. ``auto_enabled`` is the only switch
    that lets a rate move without somebody pressing a button, and a fresh row
    has it off: the first rates this writes should be ones somebody watched.

    The percentages are all of the owner's CURRENT guest price for the room:
    ``max_step_pct`` caps one move, ``floor_pct``/``ceiling_pct`` bound where
    the rate may end up at all. ``position_pct`` leans the rule off our usual
    place against the market -- 0 keeps the usual gap, -5 sits five per cent
    cheaper than usual.
    """

    __tablename__ = "repricing_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    auto_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    position_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"), nullable=False)
    max_step_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("10"), nullable=False)
    floor_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("30"), nullable=False)
    ceiling_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("50"), nullable=False)
    #: Fewer competitors than this and no proposal is made: a "market" of one
    #: hotel is that hotel's price with a new name.
    min_competitors: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    #: Proposals are rounded to a multiple of this, so a rate reads like one.
    round_to: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    #: The channel row in RMS's Room Rate Manager the rates are written under.
    channel: Mapped[str] = mapped_column(String(120), default="Booking.com", nullable=False)
    #: Demand lifts on top of the usual gap (see services/repricing.demand):
    #: Friday and Saturday nights, and the whole tier sold out (scaled by
    #: the share that is).
    weekend_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"), nullable=False)
    sold_out_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("10"), nullable=False)
    #: Every channel the RMS grid listed on the last visit, ``channel`` among
    #: them. The others are only ever written when the owner ticks them for a
    #: room on the Repricing page -- never by the automatic run.
    known_channels: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: The ONE competitor this owner prices against, for an owner who prices
    #: off a single property rather than off the hill. NULL -- the default --
    #: and the median rule applies as it always has.
    #:
    #: It drives BOTH the proposed price (services/repricing.Benchmark: their
    #: entry price for the tier, less ``benchmark_undercut``) and what the
    #: advisor is shown. One picker, because they are one decision: an owner
    #: who prices against Sterling does not also want a second opinion about
    #: a market they have stopped reading.
    #:
    #: SET NULL on delete, not CASCADE: losing the benchmarked hotel must
    #: cost the owner their benchmark, never their whole rule. The proposal
    #: then falls back to the median rule rather than stopping.
    benchmark_hotel_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotels.id", ondelete="SET NULL"), nullable=True
    )
    #: How far UNDER the benchmark to sit, in rupees. Rupees and not a
    #: percentage on purpose: the owner's rule is "a hundred less than
    #: Sterling", and a percentage of a price that moves is a gap that moves
    #: with it. Ignored entirely when no benchmark is set.
    benchmark_undercut: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("100"), nullable=False, server_default="100"
    )
    #: Whether the gap is measured on the price the GUEST pays -- the rate
    #: plus the tax line the site prints beside it -- rather than the rate
    #: alone. On by default, because that is the figure a guest compares and
    #: the tax rates are not equal: Booking.com printed 5.68% on ours and
    #: 5.01% on Sterling's on the same night, so a hundred under them before
    #: tax is not a hundred under them on screen.
    benchmark_with_tax: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    #: The board both sides are quoted on ("Breakfast", "Room Only", ...),
    #: or NULL to take each room's cheapest rate whatever it includes.
    #: Booking.com sells one room on two or three plans and the supplements
    #: are not alike -- adding breakfast cost ASG 374 and Sterling 1,350 on
    #: the same night -- so a rule that does not pin the board is comparing
    #: two different products most nights.
    benchmark_meal_plan: Mapped[str | None] = mapped_column(String(60))
    #: Which room of theirs each room of ours competes with:
    #: ``{"25": "Classic room", "27": "Mountain View Classic Room"}``.
    #: Empty keeps the tier matching the median rule uses.
    #:
    #: THEIR SIDE IS A NAME, not an id. Their rooms are rows in our
    #: ``room_types`` too, but a competitor's room can be re-discovered,
    #: renamed or retired without anyone here noticing, and a dangling id
    #: would silently stop pairing. A name is matched through
    #: ``normalize_room_name`` each night and says plainly when it stops
    #: matching.
    #:
    #: Our side is the room_type id, because it is ours and it is stable.
    #: Keys arrive as strings through JSON and are coerced on the way out.
    benchmark_room_pairs: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False, server_default=text("'{}'::jsonb")
    )


class RmsRoomMapping(Base, TimestampMixin):
    """Which row of the rate application one of the owner's rooms is.

    The booking site names the room one way and the application another, and
    the two do not even agree on which room "Deluxe" is. This row is the
    translation, one per room type of the owner's property, with the label of
    the rate row for each meal plan (``None`` where the plan is not sold on
    the channel -- RMS then has no row to write to, and none is attempted).

    ``floor_amount``/``ceiling_amount`` are absolute bounds on the RMS rate
    for this room, when the owner has set them; the percentage bounds in
    ``RepricingSettings`` apply either way.
    """

    __tablename__ = "rms_room_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    room_type_id: Mapped[int] = mapped_column(
        ForeignKey("room_types.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    rms_room: Mapped[str] = mapped_column(String(200), nullable=False)
    rate_type_ep: Mapped[str | None] = mapped_column(String(200))
    rate_type_cp: Mapped[str | None] = mapped_column(String(200))
    rate_type_map: Mapped[str | None] = mapped_column(String(200))
    floor_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    ceiling_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    room_type = relationship("RoomType")

    def rate_type_for(self, plan: str) -> str | None:
        return {"EP": self.rate_type_ep, "CP": self.rate_type_cp, "MAP": self.rate_type_map}[plan]

    @property
    def anchor_plan(self) -> str | None:
        """The plan whose RMS rate is scaled from the site price: the entry
        plan the room is sold on. EP where the room has one; a room sold only
        with breakfast (RMS's DELUXE has a CP row and nothing else) anchors on
        that, and the other plans follow it by their supplement."""
        return next((plan for plan in PLANS if self.rate_type_for(plan)), None)

    def anchor_for(self, board: str | None) -> str | None:
        """The RMS row the site price belongs to, given the board it was quoted on.

        WHY :attr:`anchor_plan` IS NOT ALWAYS THE ANSWER. It picks the entry
        plan -- EP where a room has one -- which is right when the figure
        being scaled is the room-only rate, because that is the rate the
        sites lead with. Pin the comparison to breakfast and it stops being
        right: the price is now the CP rate, and scaling the EP row by a CP
        ratio writes a number that produces neither.

        On ASG's Deluxe Double on 21 Sep that was the difference between
        Booking.com showing 5,690 and showing about 5,050.

        Falls back to :attr:`anchor_plan` when no board is pinned, or when
        the room is not sold on the one that is -- a room with only a CP row
        anchors there whatever the rule asked for, because that is the only
        rate there is to move.
        """
        wanted = rms_plan(board)
        if wanted and self.rate_type_for(wanted):
            return wanted
        return self.anchor_plan


class RepricingAction(Base):
    """One thing the repricer decided about one rate row on one night.

    ``status`` is one of:

    ``proposed``  computed and shown, nothing written (a manual run's preview)
    ``held``      outside the limits, or too few competitors; not written
    ``applied``   written to RMS and read back as the new figure
    ``unchanged`` the RMS grid already held the proposed figure
    ``failed``    the write was attempted and the grid did not take it
    ``read``      a read-only visit: ``current_rms`` is what the grid showed

    ``competitors`` is the list the market figure was computed from --
    ``[{"hotel": ..., "room": ..., "price": ...}, ...]`` -- because a rate
    that moved has to be able to say on whose account.
    """

    __tablename__ = "repricing_actions"
    __table_args__ = (Index("ix_repricing_actions_owner_time", "owner_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    room_type_id: Mapped[int | None] = mapped_column(ForeignKey("room_types.id", ondelete="SET NULL"))
    room_name: Mapped[str | None] = mapped_column(String(200))
    #: The RMS channel row this was read from or written to. NULL on rows
    #: from before other channels were handled; those are all the settings'
    #: channel (Booking.com).
    channel: Mapped[str | None] = mapped_column(String(120))
    rms_room: Mapped[str | None] = mapped_column(String(200))
    rms_rate_type: Mapped[str | None] = mapped_column(String(200))
    plan: Mapped[str | None] = mapped_column(String(8))
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)  # auto | manual | dry_run | survey
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    our_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    competitors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    current_rms: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    proposed_rms: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    applied_rms: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    reason: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RepricingAdvice(Base):
    """What the model said about one room on one night, beside what the rule said.

    THE SHADOW LOG. Nothing in this table has ever moved a rate. It exists so
    the question "is the advisor actually better than the median rule?" can be
    answered from weeks of real nights rather than from an opinion, before the
    advisor is allowed anywhere near ``aim()``.

    Both positions are stored in the SAME unit -- percent against the market
    median -- and both targets are the guest price each position produces after
    ``aim`` and ``guard``, so the two columns can be subtracted. ``rule_target``
    is what was really proposed that night; ``advisor_target`` is the road not
    taken.

    A row with ``advisor_position_pct`` NULL is a night the advisor produced
    nothing usable, and ``error`` says why. Those rows are the point, not
    noise: an advisor that fails a third of the time is not a better rule, and
    only the count of these can say so.
    """

    __tablename__ = "repricing_advice"
    __table_args__ = (
        Index("ix_repricing_advice_owner_time", "owner_user_id", "created_at"),
        Index("ix_repricing_advice_night", "check_in"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    room_type_id: Mapped[int | None] = mapped_column(ForeignKey("room_types.id", ondelete="SET NULL"))
    room_name: Mapped[str | None] = mapped_column(String(200))
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)

    #: The inputs both sides saw, so a row can be re-read without the night.
    our_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    competitors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    #: The rule: its standing position, and the target it actually proposed.
    rule_position_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    rule_target: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    #: The advisor: its position after clamping, and the target that position
    #: would have produced through the same aim() and guard().
    advisor_position_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    advisor_target: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    advisor_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    rationale: Mapped[str | None] = mapped_column(Text)
    key_factors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Set when the model's number was outside the band and pulled to its edge.
    clamped: Mapped[str | None] = mapped_column(Text)
    #: Set when there was no usable answer at all. Mutually exclusive with a
    #: position, and the reason this row still exists.
    error: Mapped[str | None] = mapped_column(Text)

    model: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
