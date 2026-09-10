"""Who gets told, and what was actually delivered."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, Time, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, pg_enum
from app.db.models.enums import NotificationStatus

if TYPE_CHECKING:
    from app.db.models.hotel import Hotel


class Recipient(Base, TimestampMixin):
    """A person who receives alerts."""

    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    # E.164, e.g. +9198xxxxxxxx. Required for WhatsApp.
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Told when the MONITORING breaks, as opposed to when a price moves.
    #
    # A separate flag rather than an implication of having assignments: the
    # person who wants to know the scraper stopped is usually not the person
    # who wants every rate move on one property, and conflating them is how
    # useful alerts get muted.
    receives_ops_alerts: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # Alerts inside this window are queued and released at quiet_hours_end,
    # so a 3 AM price move does not wake anyone.
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)

    # Follows every hotel, including ones added after this row was written.
    #
    # Evaluated at dispatch, not materialised into hotel_recipients rows: a
    # backfill would have to be re-run on every hotel creation path, and the
    # failure mode of missing one is a hotel that silently alerts nobody. The
    # dispatcher synthesises the assignment instead -- see
    # ``tasks_notify._links_by_pair``.
    #
    # A real hotel_recipients row still wins where one exists, so a per-hotel
    # threshold set by hand is not overridden by this flag.
    alerts_all_hotels: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    # Exempt from quiet hours AND from the per-recipient hourly cap.
    #
    # Deliberately one flag rather than two: both exist to stop a bulk reprice
    # becoming a hundred separate messages, and a recipient who is exempt from
    # one but not the other gets the surprising half of each. Set on the
    # WhatsApp alert numbers, which are wanted immediately at any hour.
    #
    # Digest batching still applies -- that groups one hotel's changes into a
    # single message and is what keeps this from being unusable.
    bypass_throttle: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    hotel_links: Mapped[list[HotelRecipient]] = relationship(
        back_populates="recipient", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Recipient {self.id} {self.name!r}>"


class HotelRecipient(Base, TimestampMixin):
    """Assigns a recipient to a hotel, with per-assignment channels and thresholds.

    The same person can want WhatsApp for the hotel next door and a daily email
    for one further away, so sensitivity lives on the assignment rather than on
    the person or the hotel.
    """

    __tablename__ = "hotel_recipients"
    __table_args__ = (UniqueConstraint("hotel_id", "recipient_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hotel_id: Mapped[int] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # {'email'} or {'email','whatsapp'} — plain text[] keeps adding a channel
    # a config change rather than a migration.
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), default=lambda: ["email"], nullable=False
    )

    min_delta_abs: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_delta_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    hotel: Mapped[Hotel] = relationship(back_populates="recipients")
    recipient: Mapped[Recipient] = relationship(back_populates="hotel_links")

    def __repr__(self) -> str:
        return f"<HotelRecipient hotel={self.hotel_id} recipient={self.recipient_id}>"


class Notification(Base):
    """One message sent to one person.

    ``price_change_ids`` is an array because of digest batching: a market-wide
    weekend reprice can produce a hundred changes in one cycle, and a hundred
    separate WhatsApps would get the system muted. All of a hotel's changes in
    a short window become ONE message.

    ``dedupe_key`` is a hash of (recipient, channel, sorted change ids) with a
    unique index, so a Celery retry can never double-send.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notifications_dedupe"),
        Index("ix_notifications_recipient_time", "recipient_id", "created_at"),
        Index("ix_notifications_status", "status", "created_at"),
        Index("ix_notifications_provider_msg", "provider_message_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False
    )
    hotel_id: Mapped[int | None] = mapped_column(ForeignKey("hotels.id", ondelete="SET NULL"))

    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)

    #: What this message is ABOUT -- ``price_change`` or ``market_comparison``.
    #:
    #: Stored rather than derived. A message held for quiet hours is rebuilt
    #: from this row hours later, and the rebuild has to produce the same kind
    #: of message that was queued: the setting behind it can be switched off in
    #: between, and a comparison queued at 11 PM must not be released at 7 AM
    #: as a price-change alert about whichever hotel happened to be first.
    #:
    #: It also decides which approved WhatsApp template carries it, so getting
    #: this wrong is a paid message that lands in the wrong shape rather than a
    #: cosmetic mislabel.
    kind: Mapped[str] = mapped_column(
        String(20), default="price_change", server_default="price_change", nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)

    price_change_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)

    subject: Mapped[str | None] = mapped_column(String(300))
    body_rendered: Mapped[str | None] = mapped_column(Text)

    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.QUEUED,
        nullable=False,
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set when an alert lands in quiet hours; the sender releases it at this time.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<Notification {self.id} {self.channel} {self.status}>"


class ComparisonLink(Base, TimestampMixin):
    """The page a recipient opens from the bottom of an alert.

    WHY A ROW AND NOT A SIGNED TOKEN
    ================================
    A JWT carrying the same facts is around 250 characters, and percent-encodes
    to about 350 in a query string. The whole WhatsApp message travels in one
    URL against a 2,048-byte cap -- see ``_fit_to_query_budget`` in the My
    Dreams provider, and the 404 that taught us -- so a self-describing token
    would spend a fifth of the message budget on itself and push real price
    lines into "and N more on the dashboard".

    Sixteen opaque characters cost 45 including the host, and buy two things a
    signed token cannot: the link can be revoked, and what it opens can be
    corrected after it has been sent.

    WHAT IT GRANTS
    ==============
    Read access to ONE night's comparison for ONE owner's hotels, and nothing
    else. It is not a login: the token names no user to authenticate as, the
    page it opens has no navigation, and the row is the only thing that can
    turn it into a query. Anybody the message is forwarded to can open it until
    it expires, which is the deliberate trade for a recipient who has no
    dashboard account -- and why ``expires_at`` is not optional.

    ONE ROW PER (OWNER, NIGHT, OCCUPANCY, BASELINE), NOT ONE PER MESSAGE. Four
    numbers times a two-hourly summary times ten hotels is a row every ten
    minutes, all opening the identical page. Reuse also means the link in this
    morning's message and the one in this afternoon's are the same URL, so a
    reader who kept the first still lands somewhere current.
    """

    __tablename__ = "comparison_links"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "check_in", "check_out", "adults", "baseline_hotel_id",
            name="uq_comparison_links_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    #: What appears in the URL. Opaque, so it says nothing about who or what it
    #: opens to somebody who intercepts the message.
    token: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    #: Whose hotels the page is scoped to. Every query behind it is filtered by
    #: this, exactly as the signed-in page filters by the logged-in user, so a
    #: link can never widen to somebody else's properties.
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: The night the message was about. Pinned rather than resolved at open
    #: time: an alert names last night's move, and a link that quietly rolled
    #: forward would show a reader different numbers from the ones that made
    #: them tap it.
    check_in: Mapped[date] = mapped_column(Date, nullable=False)
    check_out: Mapped[date] = mapped_column(Date, nullable=False)
    adults: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    #: Which of the owner's properties the gaps are measured against. NULL
    #: means "the first of theirs", the same fallback the signed-in page uses.
    #: SET NULL rather than CASCADE: retiring a property should not delete a
    #: link and turn every message quoting it into a dead end.
    baseline_hotel_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotels.id", ondelete="SET NULL")
    )

    #: Not nullable, and not defaulted in the database. A link with no expiry
    #: is a standing credential handed to a phone number, and the one thing
    #: nobody would notice is that it never stopped working.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<ComparisonLink {self.token} owner={self.owner_user_id}>"
