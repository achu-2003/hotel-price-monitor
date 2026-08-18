"""Who gets told, and what was actually delivered."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY, BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer,
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

    # Alerts inside this window are queued and released at quiet_hours_end,
    # so a 3 AM price move does not wake anyone.
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)

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
