"""Turning confirmed changes into messages that actually reach someone.

Three tasks:

``notify.dispatch_changes``      batch → filter → create notification rows
``notify.send``                  one row → one provider call → record the result
``notify.release_quiet_hours``   send what was held overnight

The split exists so a provider outage cannot lose a change. ``dispatch_changes``
commits the notification rows and marks the changes notified in ONE
transaction; from that point the message exists as a durable row with a status,
and ``send`` can fail, retry, or be re-run by hand from the dashboard without
any of it depending on the original fetch still being around.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.logging import get_logger
from app.core.ratelimit import consume_recipient_quota, recipient_quota_remaining
from app.db.models import (
    Hotel,
    HotelRecipient,
    Notification,
    NotificationStatus,
    PriceChange,
    PriceSeries,
    Recipient,
    RoomType,
)
from app.db.session import sync_session
from app.notifications import registry
from app.notifications.base import ChangeLine, Destination
from app.notifications.digest import (
    ChangeFacts,
    dedupe_key,
    group_for_digest,
    in_quiet_hours,
    passes_recipient_threshold,
    release_time,
)
from app.notifications.render import render_digest

log = get_logger("tasks.notify")

_MAX_SEND_ATTEMPTS = 3
_SEND_BACKOFF = (60, 300, 900)


@shared_task(name="notify.dispatch_changes", ignore_result=True)
def dispatch_changes(change_ids: list[int]) -> dict[str, int]:
    """Batch changes per (recipient, hotel) and queue one message each."""
    now = datetime.now(UTC)
    created: list[int] = []

    with sync_session() as session:
        changes = session.execute(
            select(PriceChange).where(PriceChange.id.in_(change_ids))
        ).scalars().all()
        if not changes:
            return {"notifications": 0}

        hotel_ids = {c.hotel_id for c in changes}
        assignments = _assignments_for(session, hotel_ids)
        if not assignments:
            # Nobody is assigned to these hotels. Still mark the changes
            # notified: leaving them pending would make them reappear in every
            # subsequent dispatch forever.
            for change in changes:
                change.notified = True
            log.info("no_recipients_assigned", hotels=sorted(hotel_ids))
            return {"notifications": 0}

        facts = [
            ChangeFacts(
                change_id=c.id,
                hotel_id=c.hotel_id,
                delta=c.delta,
                delta_pct=c.delta_pct,
                direction=str(c.direction),
            )
            for c in changes
        ]
        by_id = {c.id: c for c in changes}

        recipient_ids = {r for links in assignments.values() for r in links}
        recipients = {
            r.id: r
            for r in session.execute(
                select(Recipient).where(
                    Recipient.id.in_(recipient_ids), Recipient.is_active.is_(True)
                )
            ).scalars()
        }
        links = _links_by_pair(session, hotel_ids)
        hotels = {
            h.id: h
            for h in session.execute(select(Hotel).where(Hotel.id.in_(hotel_ids))).scalars()
        }
        lines_by_change = _render_lines(session, changes, hotels)

        batches = group_for_digest(facts, assignments)

        for (recipient_id, hotel_id), batch_ids in batches.items():
            recipient = recipients.get(recipient_id)
            link = links.get((hotel_id, recipient_id))
            if recipient is None or link is None or not link.is_active:
                continue

            kept = [
                cid
                for cid in batch_ids
                if passes_recipient_threshold(
                    _facts_for(by_id[cid]), link.min_delta_abs, link.min_delta_pct
                )
            ]
            if not kept:
                continue

            message = render_digest(
                hotels[hotel_id].name,
                [lines_by_change[cid] for cid in sorted(kept)],
                when=now,
            )

            for channel in link.channels or ["email"]:
                notification_id = _create_notification(
                    session,
                    recipient=recipient,
                    hotel_id=hotel_id,
                    channel=channel,
                    change_ids=sorted(kept),
                    subject=message.subject,
                    body=message.text,
                    now=now,
                )
                if notification_id is not None:
                    created.append(notification_id)

        for change in changes:
            change.notified = True

    for notification_id in created:
        send_notification.apply_async(args=[notification_id], queue="notify")

    log.info("notifications_queued", count=len(created), changes=len(change_ids))
    return {"notifications": len(created)}


def _create_notification(
    session: Session,
    *,
    recipient: Recipient,
    hotel_id: int,
    channel: str,
    change_ids: list[int],
    subject: str,
    body: str,
    now: datetime,
) -> int | None:
    """Insert the notification row, honouring quiet hours and the dedupe key.

    Returns ``None`` when the row already existed (a retry) or when the
    message is being held for later — in both cases there is nothing to send
    right now.
    """
    settings = get_settings()
    try:
        provider = registry.get_provider(channel)
    except LookupError:
        log.warning("unknown_channel", channel=channel, recipient_id=recipient.id)
        return None

    quiet_start = recipient.quiet_hours_start or settings.quiet_hours_start
    quiet_end = recipient.quiet_hours_end or settings.quiet_hours_end
    local_now = now.astimezone(_zone(recipient.timezone))

    scheduled_for = None
    if in_quiet_hours(local_now.time(), quiet_start, quiet_end):
        scheduled_for = release_time(now, quiet_end, recipient.timezone)

    notification = Notification(
        recipient_id=recipient.id,
        hotel_id=hotel_id,
        channel=channel,
        provider=provider.provider_name,
        dedupe_key=dedupe_key(recipient.id, channel, change_ids),
        price_change_ids=change_ids,
        subject=subject[:300],
        body_rendered=body,
        status=NotificationStatus.QUEUED,
        created_at=now,
        scheduled_for=scheduled_for,
    )
    session.add(notification)
    try:
        # Savepoint: a duplicate must not poison the surrounding transaction,
        # which still has the other recipients' rows to write.
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        log.info("notification_deduplicated", recipient_id=recipient.id, channel=channel)
        return None

    if scheduled_for is not None:
        log.info(
            "notification_held_for_quiet_hours",
            recipient_id=recipient.id,
            release_at=scheduled_for.isoformat(),
        )
        return None

    return notification.id


@shared_task(bind=True, name="notify.send", max_retries=_MAX_SEND_ATTEMPTS, ignore_result=True)
def send_notification(self, notification_id: int) -> dict[str, str]:
    """Deliver one notification and record what the provider said."""
    now = datetime.now(UTC)

    with sync_session() as session:
        notification = session.get(Notification, notification_id)
        if notification is None:
            return {"status": "missing"}
        if notification.status in (NotificationStatus.SENT, NotificationStatus.DELIVERED,
                                   NotificationStatus.READ):
            # Already delivered. A retry that raced the original must not send
            # a second copy.
            return {"status": str(notification.status)}

        recipient = session.get(Recipient, notification.recipient_id)
        if recipient is None or not recipient.is_active:
            notification.status = NotificationStatus.FAILED
            notification.error_code = "recipient_inactive"
            return {"status": "failed"}

        settings = get_settings()
        remaining = recipient_quota_remaining(recipient.id, settings.recipient_max_msgs_per_hour)
        if remaining <= 0:
            # Over the hourly cap. Held rather than dropped, and released by
            # the same sweep that handles quiet hours.
            notification.scheduled_for = now.replace(microsecond=0) + _one_hour()
            log.info("notification_rate_limited", recipient_id=recipient.id)
            return {"status": "deferred"}

        provider = registry.get_provider(notification.channel)
        destination = Destination(
            name=recipient.name, email=recipient.email, phone_e164=recipient.phone_e164
        )
        subject = notification.subject or ""
        body = notification.body_rendered or ""
        # Re-rendered rather than stored per channel: the stored text is the
        # audit record of what was said, while HTML and template parameters are
        # presentation and can change with a template fix.
        message = _rebuild_message(session, notification, subject, body)

        notification.attempts += 1
        result = provider.send(destination, message)

        if result.ok:
            notification.status = NotificationStatus.SENT
            notification.sent_at = now
            notification.provider_message_id = result.provider_message_id
            notification.error_code = None
            notification.error_detail = None
            consume_recipient_quota(recipient.id)
            log.info("notification_sent", notification_id=notification.id,
                     channel=notification.channel)
            return {"status": "sent"}

        notification.error_code = result.error_code
        notification.error_detail = (result.error_detail or "")[:2000]
        attempts = notification.attempts
        will_retry = result.retryable and attempts < _MAX_SEND_ATTEMPTS
        if not will_retry:
            notification.status = NotificationStatus.FAILED
            log.warning(
                "notification_failed",
                notification_id=notification.id,
                channel=notification.channel,
                error_code=result.error_code,
                attempts=attempts,
            )

    if result.retryable and attempts < _MAX_SEND_ATTEMPTS:
        raise self.retry(countdown=_SEND_BACKOFF[min(attempts - 1, len(_SEND_BACKOFF) - 1)])
    return {"status": "failed"}


@shared_task(name="notify.release_quiet_hours", ignore_result=True)
def release_quiet_hours() -> dict[str, int]:
    """Send everything whose hold has expired. Runs every five minutes.

    Covers both quiet hours and the hourly per-recipient cap, because both use
    ``scheduled_for`` to mean "not before this time".
    """
    now = datetime.now(UTC)
    with sync_session() as session:
        due = session.execute(
            select(Notification.id).where(
                Notification.status == NotificationStatus.QUEUED,
                Notification.scheduled_for.is_not(None),
                Notification.scheduled_for <= now,
            ).limit(500)
        ).scalars().all()

        for notification_id in due:
            notification = session.get(Notification, notification_id)
            if notification is not None:
                notification.scheduled_for = None

    for notification_id in due:
        send_notification.apply_async(args=[notification_id], queue="notify")

    if due:
        log.info("quiet_hours_released", count=len(due))
    return {"released": len(due)}


# -- helpers ---------------------------------------------------------
def _assignments_for(session: Session, hotel_ids: set[int]) -> dict[int, list[int]]:
    rows = session.execute(
        select(HotelRecipient.hotel_id, HotelRecipient.recipient_id).where(
            HotelRecipient.hotel_id.in_(hotel_ids), HotelRecipient.is_active.is_(True)
        )
    ).all()
    assignments: dict[int, list[int]] = {}
    for hotel_id, recipient_id in rows:
        assignments.setdefault(hotel_id, []).append(recipient_id)
    return assignments


def _links_by_pair(session: Session, hotel_ids: set[int]) -> dict[tuple[int, int], HotelRecipient]:
    rows = session.execute(
        select(HotelRecipient).where(HotelRecipient.hotel_id.in_(hotel_ids))
    ).scalars().all()
    return {(link.hotel_id, link.recipient_id): link for link in rows}


def _render_lines(
    session: Session, changes: list[PriceChange], hotels: dict[int, Hotel]
) -> dict[int, ChangeLine]:
    """Join each change to the room and stay it belongs to.

    One query for the series rows rather than one per change: a weekend-wide
    reprice can carry a hundred changes, and a hundred round trips inside the
    notification path is how alerts start arriving minutes late.
    """
    offer_keys = {c.offer_key for c in changes}
    series = {
        s.offer_key: s
        for s in session.execute(
            select(PriceSeries).where(PriceSeries.offer_key.in_(offer_keys))
        ).scalars()
    }
    room_ids = {s.room_type_id for s in series.values()}
    rooms = {
        r.id: r
        for r in session.execute(select(RoomType).where(RoomType.id.in_(room_ids))).scalars()
    }

    lines: dict[int, ChangeLine] = {}
    for change in changes:
        entry = series.get(change.offer_key)
        room = rooms.get(entry.room_type_id) if entry else None
        lines[change.id] = ChangeLine(
            hotel_name=hotels[change.hotel_id].name if change.hotel_id in hotels else "Unknown",
            room_name=room.name if room else "(room)",
            old_price=change.old_price,
            new_price=change.new_price,
            delta=change.delta,
            delta_pct=change.delta_pct,
            currency=change.currency,
            direction=str(change.direction),
            check_in=entry.check_in.isoformat() if entry else "",
            check_out=entry.check_out.isoformat() if entry else "",
            meal_plan=entry.meal_plan if entry else None,
        )
    return lines


def _rebuild_message(session: Session, notification: Notification, subject: str, body: str):
    """Reconstruct the rendered message for a notification about to be sent.

    Rebuilt from the change ids rather than stored per channel, so a template
    fix applies to a message that has been sitting in a quiet-hours hold since
    last night.
    """
    changes = session.execute(
        select(PriceChange).where(PriceChange.id.in_(notification.price_change_ids))
    ).scalars().all()
    if not changes:
        from app.notifications.base import RenderedMessage

        return RenderedMessage(subject=subject, text=body, html=None)

    hotels = {
        h.id: h
        for h in session.execute(
            select(Hotel).where(Hotel.id.in_({c.hotel_id for c in changes}))
        ).scalars()
    }
    lines = _render_lines(session, changes, hotels)
    hotel_name = hotels[changes[0].hotel_id].name if changes[0].hotel_id in hotels else "Hotel"
    return render_digest(
        hotel_name, [lines[c.id] for c in changes], when=notification.created_at
    )


def _facts_for(change: PriceChange) -> ChangeFacts:
    return ChangeFacts(
        change_id=change.id,
        hotel_id=change.hotel_id,
        delta=change.delta,
        delta_pct=change.delta_pct,
        direction=str(change.direction),
    )


def _zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Kolkata")


def _one_hour():
    return timedelta(hours=1)
