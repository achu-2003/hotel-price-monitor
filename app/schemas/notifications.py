"""Recipients, assignments, and the delivery record."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from pydantic import EmailStr, Field, model_validator

from app.db.models.enums import NotificationStatus
from app.schemas.common import ORMModel


class RecipientBase(ORMModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    phone_e164: str | None = Field(
        default=None,
        pattern=r"^\+[1-9]\d{7,14}$",
        description="E.164 with the country code, e.g. +919876543210. Required "
                    "for WhatsApp; the Cloud API rejects anything else.",
    )
    timezone: str = "Asia/Kolkata"
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None

    @model_validator(mode="after")
    def _reachable_somehow(self):
        if not self.email and not self.phone_e164:
            raise ValueError(
                "A recipient needs an email address or a phone number — "
                "otherwise there is no way to tell them anything."
            )
        return self


class RecipientCreate(RecipientBase):
    pass


class RecipientUpdate(ORMModel):
    name: str | None = None
    email: EmailStr | None = None
    phone_e164: str | None = None
    timezone: str | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    is_active: bool | None = None


class RecipientOut(RecipientBase):
    id: int
    is_active: bool
    hotels_assigned: int = 0


class HotelRecipientIn(ORMModel):
    """Assigning a person to a hotel, with their own channels and sensitivity.

    Thresholds live here rather than on the person or the hotel because the
    same person can want an immediate WhatsApp for the property next door and
    a quieter email for one across the valley.
    """

    recipient_id: int
    channels: list[str] = Field(default_factory=lambda: ["email"], min_length=1)
    min_delta_abs: Decimal | None = Field(default=None, ge=0)
    min_delta_pct: Decimal | None = Field(default=None, ge=0, le=100)


class HotelRecipientOut(ORMModel):
    id: int
    hotel_id: int
    recipient_id: int
    recipient_name: str | None = None
    channels: list[str]
    min_delta_abs: Decimal | None
    min_delta_pct: Decimal | None
    is_active: bool


class NotificationOut(ORMModel):
    id: int
    recipient_id: int
    recipient_name: str | None = None
    hotel_id: int | None
    hotel_name: str | None = None
    channel: str
    provider: str
    price_change_ids: list[int]
    subject: str | None
    status: NotificationStatus
    provider_message_id: str | None
    error_code: str | None
    error_detail: str | None
    attempts: int
    created_at: datetime
    scheduled_for: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None


class TestNotificationIn(ORMModel):
    """Send a sample alert, to prove the channel works before it is needed.

    Uses fabricated but clearly-labelled data. The point is to exercise the
    provider, the credentials and the template — the parts that fail silently
    until the first real price move.
    """

    recipient_id: int
    channel: str = "email"


class WhatsAppWebhookStatus(ORMModel):
    """One status callback from Meta.

    Delivery is not the same as acceptance: the send call returning 200 means
    Meta took the message, and this is how we learn whether it arrived.
    """

    message_id: str
    status: str
    timestamp: datetime | None = None
    error_code: str | None = None
