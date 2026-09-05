"""Price identity, history, and detected changes.

THE CENTRAL IDEA
================
A price is not "hotel X costs Y". It is an **offer**, identified by::

    offer_key = sha256(hotel | source | room_type | check_in | check_out
                       | adults | children | meal_plan | refundable | currency)

Two prices are comparable ONLY when their ``offer_key`` matches. Because the
key is the primary key of :class:`PriceSeries`, comparing mismatched booking
conditions is structurally impossible rather than merely discouraged.

WHY TWO TABLES
==============
:class:`PriceObservation` is append-only and authoritative: every check writes
a row, which is what gives you the 10:00 / 10:30 / 11:00 history.

:class:`PriceSeries` holds one row per ``offer_key`` with the last known price.
It exists purely for speed: answering "what was the previous price?" from the
observation table would mean ``ORDER BY checked_at DESC LIMIT 1`` on a
forever-growing table, ~360 times every cycle. Here it is one primary-key
lookup. It is a cache and can be rebuilt from observations at any time
(``scripts/rebuild_series.py``).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pg_enum
from app.db.models.enums import ChangeDirection, PriceBasis

OFFER_KEY_LEN = 64  # sha256 hex digest


class PriceSeries(Base):
    """One row per offer: the current price, plus debounce state.

    Also the backing table for the dashboard's "current prices" view, so that
    screen never touches the history table.
    """

    __tablename__ = "price_series"
    __table_args__ = (
        Index("ix_price_series_hotel_dates", "hotel_id", "check_in", "check_out"),
        Index("ix_price_series_last_changed", "last_changed_at"),
        Index("ix_price_series_stale", "last_checked_at"),
    )

    offer_key: Mapped[str] = mapped_column(String(OFFER_KEY_LEN), primary_key=True)

    # ── the booking conditions the key is derived from ───────────────
    # Denormalised on purpose: every dashboard query filters on these, and
    # they are immutable for a given offer_key.
    hotel_id: Mapped[int] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_type_id: Mapped[int] = mapped_column(
        ForeignKey("room_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)
    adults: Mapped[int] = mapped_column(Integer, nullable=False)
    children: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meal_plan: Mapped[str | None] = mapped_column(String(60))
    refundable: Mapped[bool | None] = mapped_column(Boolean)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # ── current state ────────────────────────────────────────────────
    # TWO PRICES, AND THEY ANSWER DIFFERENT QUESTIONS
    # ==============================================
    # ``current_price``  what the hotel is asking RIGHT NOW -- every check
    #                    overwrites it, unconditionally. This is the number to
    #                    show a person, because it is the one they will see if
    #                    they open the hotel's own booking page.
    # ``last_price``     the CONFIRMED baseline the change detector compares
    #                    against. It deliberately does not move for a wobble
    #                    below the alert threshold, so that a run of small
    #                    drifts accumulates against one fixed point and
    #                    eventually alerts, instead of each step being waved
    #                    through relative to the last.
    #
    # Displaying ``last_price`` conflated the two, and the dashboard drifted
    # away from the hotel's own page and stayed there: a 2.8% drop that missed
    # the 50-rupee floor was recorded correctly in price_observations, left
    # last_price untouched, and so never reached the screen. The gap only ever
    # widened, because nothing short of a threshold-clearing move closed it.
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_price_basis: Mapped[PriceBasis] = mapped_column(
        pg_enum(PriceBasis, "price_basis"), default=PriceBasis.INCLUSIVE, nullable=False
    )

    # ── what the site actually published, kept for the display switch ──
    # ``current_price`` is one number on ONE basis, chosen by PRICE_BASIS, and
    # it is the number the change detector runs on. It cannot answer "show me
    # this with tax", because for seven of the ten hotels here the tax is a
    # separate figure and for three there is no pre-tax figure at all.
    #
    # So the components are carried alongside it, written by ingest from the
    # same offer. NULL means the site did not say -- never zero, which would
    # claim a room is taxed at nothing. See services/price_display.py for how
    # a missing component is rendered rather than guessed at.
    last_price_exclusive: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_taxes_fees: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_price_inclusive: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── debounce state (plan section 9, step 7) ──────────────────────
    # A new price must persist across ``confirm_checks`` consecutive checks
    # before it counts. This is what stops dynamic-pricing noise and A/B tests
    # from generating alerts you would stop trusting.
    pending_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── the same debounce, for the room going away ───────────────────
    # When this room was first NOT on a page that still listed other rooms.
    # Cleared the moment it is seen again, so a value here always means "absent
    # on every check since". A disappearance is confirmed on the second such
    # check; see ``ingest._handle_disappearances`` for why absence needs the
    # debounce that a positively declared sold-out does not.
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<PriceSeries {self.offer_key[:12]}... {self.currency} {self.current_price}>"


class PriceObservation(Base):
    """Append-only record of every check. This is the price history.

    Partitioned monthly on ``checked_at``: at 30 hotels this is ~500k rows a
    month, at 500 hotels ~8.6M. Partitioning from day one means retention is a
    ``DROP PARTITION`` instead of a long ``DELETE``.

    ``PRIMARY KEY (id, checked_at)``: PostgreSQL requires the partition key to
    be part of every unique constraint.
    """

    __tablename__ = "price_observations"
    __table_args__ = (
        # One observation per offer per run: makes a Celery retry idempotent.
        UniqueConstraint("offer_key", "checked_at", name="uq_observation_offer_time"),
        Index("ix_observations_offer_time", "offer_key", "checked_at"),
        Index("ix_observations_run", "check_run_id"),
        CheckConstraint(
            "price_inclusive IS NULL OR price_inclusive >= 0",
            name="price_inclusive_non_negative",
        ),
        {"postgresql_partition_by": "RANGE (checked_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    offer_key: Mapped[str] = mapped_column(String(OFFER_KEY_LEN), nullable=False)

    # All three components are stored so the comparison basis can be changed
    # later without losing information, and so a hotel quoting
    # "2500 + taxes" is comparable to one quoting "2950 inclusive".
    price_exclusive: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    taxes_fees: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_inclusive: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rooms_left: Mapped[int | None] = mapped_column(Integer)

    # What the site actually called this room, kept verbatim. When a mapping
    # turns out wrong, this is how the series is repaired.
    raw_room_name: Mapped[str | None] = mapped_column(String(300))
    # Scrubbed through app.core.redaction before it is written.
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)

    check_run_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))

    def __repr__(self) -> str:
        return f"<PriceObservation {self.offer_key[:12]}... @{self.checked_at:%Y-%m-%d %H:%M}>"


#: Why a confirmed change told nobody. Each value maps to a different fix,
#: which is the whole point of recording them separately: "assign somebody",
#: "reactivate them", and "your threshold is too high" are three different
#: conversations that a single ``notified`` flag made indistinguishable.
SUPPRESSED_NO_RECIPIENTS = "no_recipients"       # nobody is assigned to the hotel
SUPPRESSED_RECIPIENT_INACTIVE = "recipient_inactive"  # assigned, but switched off
SUPPRESSED_BELOW_THRESHOLD = "below_threshold"   # too small for everyone assigned

SUPPRESSION_LABELS = {
    SUPPRESSED_NO_RECIPIENTS: "nobody is assigned to this hotel",
    SUPPRESSED_RECIPIENT_INACTIVE: "everyone assigned is deactivated",
    SUPPRESSED_BELOW_THRESHOLD: "below everyone's alert threshold",
}


class PriceChange(Base):
    """A confirmed change. One row here is one thing worth telling someone.

    Written only after the significance threshold AND the debounce have both
    passed, so this table is the honest answer to "what actually changed?".
    """

    __tablename__ = "price_changes"
    __table_args__ = (
        Index("ix_price_changes_hotel_time", "hotel_id", "changed_at"),
        Index("ix_price_changes_unnotified", "notified", "changed_at"),
        Index("ix_price_changes_offer", "offer_key", "changed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    offer_key: Mapped[str] = mapped_column(String(OFFER_KEY_LEN), nullable=False)
    hotel_id: Mapped[int] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # When the new price was FIRST seen, as opposed to when it was confirmed.
    #
    # A change is only written on its second consecutive sighting, so
    # ``changed_at`` is the moment the debounce completed -- on a 30-minute
    # target that is up to an hour after the hotel actually moved its rate.
    # Reporting only that time answers "when did we finish checking", which is
    # not the question anybody asks of this table.
    #
    # Nullable: rows written before this existed cannot have it, and inventing
    # a value for them would be worse than the honest gap.
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    delta: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    delta_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # BECAME_UNAVAILABLE is a distinct direction, never "price dropped to 0".
    direction: Mapped[ChangeDirection] = mapped_column(
        pg_enum(ChangeDirection, "change_direction"), nullable=False
    )

    observation_id_old: Mapped[int | None] = mapped_column(BigInteger)
    observation_id_new: Mapped[int | None] = mapped_column(BigInteger)

    # Set only when this row came from comparing two DIFFERENT stay dates --
    # tonight's opening price against last night's closing price for the same
    # room. NULL means the ordinary intraday comparison, where both prices
    # belong to one offer_key. Storing the key rather than a bare flag means
    # the baseline can always be traced back to the series it came from.
    previous_offer_key: Mapped[str | None] = mapped_column(String(OFFER_KEY_LEN))

    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Why this change told nobody, when it told nobody.
    #
    # ``notified`` has to be set even when there was no one to send to, or the
    # change reappears in every dispatch sweep forever. That made "delivered"
    # and "reached nobody" the same state from the outside. This says which.
    #
    # NULL means sent, or not dispatched yet -- ``notified`` separates those.
    suppressed_reason: Mapped[str | None] = mapped_column(String(32))

    def __repr__(self) -> str:
        return f"<PriceChange {self.direction} {self.old_price}->{self.new_price}>"


class UnmatchedOffer(Base, TimestampMixin):
    """A room name no alias resolved.

    The system never guesses: a wrong mapping silently corrupts a price series
    indefinitely, which is far worse than a gap. These surface in the dashboard
    for a human to map once, after which the alias handles it forever.
    """

    __tablename__ = "unmatched_offers"
    __table_args__ = (
        UniqueConstraint("hotel_source_id", "normalized_name"),
        Index("ix_unmatched_open", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_source_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_room_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    sample_payload: Mapped[dict | None] = mapped_column(JSONB)

    # Best fuzzy candidate, shown to the operator as a suggestion. Recorded,
    # never auto-applied.
    suggested_room_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("room_types.id", ondelete="SET NULL")
    )
    suggested_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<UnmatchedOffer {self.raw_room_name!r} x{self.occurrence_count}>"
