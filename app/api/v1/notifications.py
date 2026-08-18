"""Recipients, assignments, delivery history, and the WhatsApp status webhook."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, DbSession, get_object_or_404, record_audit
from app.config import get_settings
from app.core.logging import get_logger
from app.db.models import (
    Hotel,
    HotelRecipient,
    Notification,
    NotificationStatus,
    Recipient,
)
from app.notifications import registry
from app.schemas.common import Page
from app.schemas.notifications import (
    HotelRecipientIn,
    HotelRecipientOut,
    NotificationOut,
    RecipientCreate,
    RecipientOut,
    RecipientUpdate,
    TestNotificationIn,
)

router = APIRouter(tags=["notifications"])
log = get_logger("api.notifications")


@router.get("/recipients", response_model=Page[RecipientOut])
async def list_recipients(session: DbSession, _user: CurrentUser, active: bool | None = None):
    statement = select(Recipient)
    if active is not None:
        statement = statement.where(Recipient.is_active.is_(active))
    recipients = (await session.scalars(statement.order_by(Recipient.name))).all()

    counts = dict(
        (
            await session.execute(
                select(HotelRecipient.recipient_id, func.count(HotelRecipient.id))
                .where(HotelRecipient.is_active.is_(True))
                .group_by(HotelRecipient.recipient_id)
            )
        ).all()
    )

    items = []
    for recipient in recipients:
        out = RecipientOut.model_validate(recipient)
        out.hotels_assigned = counts.get(recipient.id, 0)
        items.append(out)
    return Page[RecipientOut](items=items, total=len(items))


@router.post("/recipients", response_model=RecipientOut, status_code=status.HTTP_201_CREATED)
async def create_recipient(
    payload: RecipientCreate, request: Request, session: DbSession, admin: AdminUser
):
    recipient = Recipient(**payload.model_dump())
    session.add(recipient)
    await session.flush()
    await record_audit(
        session, user=admin, action="create", entity="recipient", entity_id=recipient.id,
        after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    return RecipientOut.model_validate(recipient)


@router.patch("/recipients/{recipient_id}", response_model=RecipientOut)
async def update_recipient(
    recipient_id: int,
    payload: RecipientUpdate,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    recipient = await get_object_or_404(session, Recipient, recipient_id, "Recipient")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(recipient, field, value)

    await record_audit(
        session, user=admin, action="update", entity="recipient",
        entity_id=recipient_id, after=data, request=request,
    )
    await session.commit()
    return RecipientOut.model_validate(recipient)


@router.post(
    "/hotels/{hotel_id}/recipients",
    response_model=HotelRecipientOut,
    status_code=status.HTTP_201_CREATED,
)
async def assign_recipient(
    hotel_id: int,
    payload: HotelRecipientIn,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Assign a person to a hotel, with their channels and sensitivity.

    Channels are validated against what is actually configured. Assigning
    someone to WhatsApp before the access token exists would otherwise look
    like it worked and fail silently at the first price move — which is the
    worst possible moment to discover it.
    """
    await get_object_or_404(session, Hotel, hotel_id, "Hotel")
    recipient = await get_object_or_404(
        session, Recipient, payload.recipient_id, "Recipient"
    )

    ready = set(registry.available_channels())
    unknown = [c for c in payload.channels if c not in registry.KNOWN_CHANNELS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel(s): {unknown}. Known: {list(registry.KNOWN_CHANNELS)}",
        )
    unconfigured = [c for c in payload.channels if c not in ready]
    if unconfigured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Channel(s) {unconfigured} are not configured on this deployment. "
                f"Currently available: {sorted(ready) or 'none'}."
            ),
        )
    if "whatsapp" in payload.channels and not recipient.phone_e164:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This recipient has no E.164 phone number, so WhatsApp cannot reach them.",
        )
    if "email" in payload.channels and not recipient.email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This recipient has no email address.",
        )

    existing = await session.scalar(
        select(HotelRecipient).where(
            HotelRecipient.hotel_id == hotel_id,
            HotelRecipient.recipient_id == payload.recipient_id,
        )
    )
    if existing is not None:
        # Re-assigning is how channels and thresholds get edited, so update
        # rather than refuse with a 409 the dashboard would have to work around.
        for field, value in payload.model_dump(exclude={"recipient_id"}).items():
            setattr(existing, field, value)
        existing.is_active = True
        await session.commit()
        out = HotelRecipientOut.model_validate(existing)
        out.recipient_name = recipient.name
        return out

    link = HotelRecipient(hotel_id=hotel_id, **payload.model_dump())
    session.add(link)
    await session.flush()
    await record_audit(
        session, user=admin, action="assign", entity="hotel_recipient", entity_id=link.id,
        after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()

    out = HotelRecipientOut.model_validate(link)
    out.recipient_name = recipient.name
    return out


@router.delete(
    "/hotels/{hotel_id}/recipients/{recipient_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def unassign_recipient(
    hotel_id: int, recipient_id: int, request: Request, session: DbSession, admin: AdminUser
):
    link = await session.scalar(
        select(HotelRecipient).where(
            HotelRecipient.hotel_id == hotel_id, HotelRecipient.recipient_id == recipient_id
        )
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That assignment does not exist."
        )
    await session.delete(link)
    await record_audit(
        session, user=admin, action="unassign", entity="hotel_recipient",
        entity_id=link.id, request=request,
    )
    await session.commit()


@router.get("/notifications", response_model=Page[NotificationOut])
async def list_notifications(
    session: DbSession,
    _user: CurrentUser,
    status_filter: NotificationStatus | None = Query(default=None, alias="status"),
    recipient_id: int | None = None,
    hotel_id: int | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    statement = (
        select(Notification, Recipient.name, Hotel.name)
        .join(Recipient, Notification.recipient_id == Recipient.id)
        .outerjoin(Hotel, Notification.hotel_id == Hotel.id)
    )
    if status_filter is not None:
        statement = statement.where(Notification.status == status_filter)
    if recipient_id is not None:
        statement = statement.where(Notification.recipient_id == recipient_id)
    if hotel_id is not None:
        statement = statement.where(Notification.hotel_id == hotel_id)
    if from_ is not None:
        statement = statement.where(Notification.created_at >= from_)

    rows = (
        await session.execute(
            statement.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    items = []
    for notification, recipient_name, hotel_name in rows:
        out = NotificationOut.model_validate(notification)
        out.recipient_name = recipient_name
        out.hotel_name = hotel_name
        items.append(out)
    return Page[NotificationOut](items=items)


@router.post("/notifications/{notification_id}/resend", status_code=status.HTTP_202_ACCEPTED)
async def resend_notification(
    notification_id: int, request: Request, session: DbSession, admin: AdminUser
):
    """Re-queue a failed message after fixing whatever broke.

    Only failed messages can be resent. Re-sending one that succeeded would
    defeat the deduplication that exists precisely to stop double-sends.
    """
    notification = await get_object_or_404(
        session, Notification, notification_id, "Notification"
    )
    if notification.status != NotificationStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed notifications can be resent; this one is "
                   f"{notification.status.value}.",
        )

    notification.status = NotificationStatus.QUEUED
    notification.attempts = 0
    notification.error_code = None
    notification.error_detail = None
    notification.scheduled_for = None

    await record_audit(
        session, user=admin, action="resend", entity="notification",
        entity_id=notification_id, request=request,
    )
    await session.commit()

    from app.workers.tasks_notify import send_notification

    send_notification.apply_async(args=[notification_id], queue="notify")
    return {"status": "queued", "notification_id": notification_id}


@router.post("/notifications/test", status_code=status.HTTP_202_ACCEPTED)
async def send_test_notification(
    payload: TestNotificationIn, request: Request, session: DbSession, admin: AdminUser
):
    """Prove a channel works before a real price move depends on it.

    Sent synchronously and the provider's own verdict is returned, because the
    point is to see the failure immediately rather than to go and look for it
    in the notification list afterwards.
    """
    recipient = await get_object_or_404(
        session, Recipient, payload.recipient_id, "Recipient"
    )
    try:
        provider = registry.get_provider(payload.channel)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if not provider.is_configured():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The {payload.channel} provider ({provider.provider_name}) is "
                   f"not configured on this deployment.",
        )

    from decimal import Decimal

    from app.notifications.base import ChangeLine, Destination
    from app.notifications.render import render_digest

    sample = ChangeLine(
        hotel_name="Sample Resort (test)",
        room_name="Deluxe Room",
        old_price=Decimal("3000"),
        new_price=Decimal("2700"),
        delta=Decimal("-300"),
        delta_pct=Decimal("-10.00"),
        currency="INR",
        direction="decrease",
        check_in="2026-08-20",
        check_out="2026-08-21",
        meal_plan="Breakfast Included",
    )
    message = render_digest("Sample Resort (test)", [sample])
    result = provider.send(
        Destination(
            name=recipient.name, email=recipient.email, phone_e164=recipient.phone_e164
        ),
        message,
    )

    await record_audit(
        session, user=admin, action="test_notification", entity="recipient",
        entity_id=recipient.id,
        after={"channel": payload.channel, "ok": result.ok, "error": result.error_code},
        request=request,
    )
    await session.commit()

    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{provider.provider_name} refused the message: "
                   f"{result.error_code} — {result.error_detail}",
        )
    return {"status": "sent", "provider_message_id": result.provider_message_id}


# -- WhatsApp delivery webhook ---------------------------------------
@router.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    request: Request,
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
):
    """Meta's one-time subscription handshake.

    Unauthenticated by necessity — Meta calls it before any session exists —
    so the shared verify token is the only thing standing between this and an
    open endpoint, and it is compared in constant time.
    """
    import hmac

    settings = get_settings()
    expected = (
        settings.whatsapp_webhook_verify_token.get_secret_value()
        if settings.whatsapp_webhook_verify_token
        else ""
    )
    if not expected or not hmac.compare_digest(hub_verify_token, expected):
        log.warning("whatsapp_webhook_verify_rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad verify token.")
    return Response(content=hub_challenge, media_type="text/plain")


@router.post("/webhooks/whatsapp", status_code=status.HTTP_200_OK)
async def whatsapp_status_webhook(request: Request, session: DbSession):
    """Record delivery, read receipts, and failures reported by Meta.

    Always returns 200, even for a payload we cannot parse. Meta retries
    anything else with escalating frequency, and a parse bug on our side would
    turn into a retry storm rather than a log line.
    """
    try:
        body = await request.json()
    except ValueError:
        return {"status": "ignored"}

    updated = 0
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for record in (change.get("value", {}) or {}).get("statuses", []) or []:
                message_id = record.get("id")
                state = (record.get("status") or "").lower()
                if not message_id:
                    continue

                notification = await session.scalar(
                    select(Notification).where(
                        Notification.provider_message_id == message_id
                    )
                )
                if notification is None:
                    continue

                if state == "delivered":
                    notification.status = NotificationStatus.DELIVERED
                    notification.delivered_at = datetime.now(UTC)
                elif state == "read":
                    notification.status = NotificationStatus.READ
                elif state == "failed":
                    notification.status = NotificationStatus.FAILED
                    errors = record.get("errors") or [{}]
                    notification.error_code = str(errors[0].get("code", "unknown"))
                    notification.error_detail = str(errors[0].get("title", ""))[:2000]
                updated += 1

    if updated:
        await session.commit()
        log.info("whatsapp_statuses_recorded", count=updated)
    return {"status": "ok", "updated": updated}
