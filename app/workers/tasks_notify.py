"""Turning confirmed changes into messages that actually reach someone.

Four tasks:

``notify.dispatch_changes``      batch → filter → create notification rows
``notify.market_summary``        every N hours → one counted window per person
``notify.send``                  one row → one provider call → record the result
``notify.release_quiet_hours``   send what was held overnight

TWO MESSAGES, TWO TRIGGERS
==========================
``dispatch_changes`` fires the moment a change is confirmed and sends one
message per (recipient, hotel) naming the rooms that moved and by how much.

Both are about RATES. A room selling out or coming back is confirmed, stored
and shown on the dashboard, and neither message carries it — see
``_PRICE_MOVE_DIRECTIONS``.

``market_summary`` fires on a clock — every ``alert_defaults``
``summary_interval_hours`` — and sends ONE message per recipient: how many
rooms moved in that window, which ones, and by how much. Not the comparison
grid: a message about a window is about what changed in it, and /comparison
answers the other question on a screen with room for a table.

Both go out. They answer different questions: one says a room moved, the other
says what the last two hours added up to and where it leaves you. Every filter
in front of the first is in front of the second too — a change still has to be
confirmed, still has to clear the recipient's threshold, and a recipient with
no live assignment still hears nothing.

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
    ChangeDirection,
    Hotel,
    HotelRecipient,
    Notification,
    NotificationStatus,
    PriceChange,
    PriceSeries,
    Recipient,
    RoomType,
)
from app.db.models.price import (
    SUPPRESSED_BELOW_THRESHOLD,
    SUPPRESSED_NOT_A_PRICE_MOVE,
    SUPPRESSED_NO_RECIPIENTS,
    SUPPRESSED_RECIPIENT_INACTIVE,
)
from app.db.session import sync_session
from app.notifications import registry
from app.notifications.base import (
    MARKET_COMPARISON,
    PRICE_CHANGE,
    WHATSAPP_COMPARISON_MIN_PARAMS,
    ChangeLine,
    Destination,
    RenderedMessage,
)
from app.notifications.digest import (
    ChangeFacts,
    dedupe_key,
    group_for_digest,
    in_quiet_hours,
    ops_dedupe_key,
    passes_recipient_threshold,
    release_time,
    summary_dedupe_key,
)
from app.notifications.render import render_digest, render_summary
from app.services import monitoring as monitoring_service
from app.services.price_display import Components, displayed_move

log = get_logger("tasks.notify")

#: How many times a RETRYABLE send is tried, and how long to wait between.
#:
#: The old budget was three attempts over 60, 300 and 900 seconds -- twenty-one
#: minutes -- and that is shorter than the outages this actually has to
#: survive. A DNS failure lasting half an hour burned all three attempts and
#: filed the alert as permanently failed: eight of them in one morning here,
#: four email and four WhatsApp, every one reading
#: "[Errno 11001] getaddrinfo failed" -- a router, not a rejection.
#:
#: Extended to five attempts across about four hours, which covers an outage
#: long enough for somebody to have gone home. Nothing else changes: only
#: errors the provider marked ``retryable`` are retried at all, so a rejected
#: address or a bad template still fails on the first attempt, and every
#: attempt is still counted and recorded on the row.
#:
#: The last entry is reused if attempts ever exceed the tuple, so the two do
#: not have to be kept the same length by hand.
_MAX_SEND_ATTEMPTS = 5
_SEND_BACKOFF = (60, 300, 900, 3600, 10800)


#: What BOTH messages report on: rates that moved, and nothing else.
#:
#: A sell-out and a return are real events, recorded and shown on the
#: dashboard. They are not rate moves, and neither message is about them:
#:
#:     Room: Club Room
#:     Was: ₹7139   Now: ₹7139   Change: now available
#:
#: — a paid WhatsApp whose two prices are the same number. And in the
#: two-hourly digest they crowd out what it is for: one real window carried
#: fifteen availability notices against three price lines, "Anthapuram Suite
#: with 2 Bedrooms, 1 Living room and Private Swimming Pool - sold out" among
#: them, each a full line saying a room is unavailable now.
#:
#: The cost is not only the reading. Slots are few and the digest is capped by
#: the URL it travels in, so every availability line was a price move that got
#: truncated or pushed into "and N more on the dashboard".
_PRICE_MOVE_DIRECTIONS = (ChangeDirection.INCREASE, ChangeDirection.DECREASE)


def _channels_in_use(channels: list[str], email_enabled: bool) -> list[str]:
    """A recipient's channels, minus the ones switched off deployment-wide.

    The recipient's own choice is a standing preference and this is a kill
    switch over all of them at once -- for the morning an inbox is drowning or
    a mail provider starts bouncing, when the answer has to be one press rather
    than an edit per recipient and an edit back afterwards.

    A recipient left with NO channel gets no message, which is the honest
    outcome: somebody on email only, with email switched off, has asked for
    nothing to be sent. It is not silent -- the caller logs the count -- and it
    reverses the moment the switch goes back on.
    """
    if email_enabled:
        return list(channels)
    return [c for c in channels if c != "email"]


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

        # A sell-out and a return are not rate moves, and these alerts are
        # about rate moves. "Was ₹7139 / Now ₹7139 / Change: now available" is
        # a paid message whose two prices are the same number.
        #
        # Marked notified rather than skipped. A change left pending comes
        # back in every later dispatch forever -- the trap the no-recipients
        # branch below is written around -- and the reason is recorded so a
        # room going quiet is never read afterwards as one nobody was
        # assigned to. It stays on the dashboard, where availability belongs.
        availability = [c for c in changes if c.direction not in _PRICE_MOVE_DIRECTIONS]
        for change in availability:
            change.notified = True
            change.suppressed_reason = SUPPRESSED_NOT_A_PRICE_MOVE
        if availability:
            log.info("availability_changes_not_alerted", count=len(availability))

        changes = [c for c in changes if c.direction in _PRICE_MOVE_DIRECTIONS]
        if not changes:
            return {"notifications": 0}

        hotel_ids = {c.hotel_id for c in changes}
        assignments = _assignments_for(session, hotel_ids)
        if not assignments:
            # Nobody live to tell. Still mark the changes notified: leaving
            # them pending would make them reappear in every subsequent
            # dispatch forever. Record WHY, though -- otherwise this is
            # indistinguishable from a delivered alert after the fact, and a
            # deployment where nobody was ever assigned looks like a healthy one.
            #
            # "None at all" and "all switched off" are separated here because
            # they send an operator to different places: one to create an
            # assignment, the other to reactivate the one already sitting on
            # the recipient's row. Reporting the second as the first invites a
            # duplicate assignment that will not work either.
            reason = (
                SUPPRESSED_RECIPIENT_INACTIVE
                if _any_assignment_exists(session, hotel_ids)
                else SUPPRESSED_NO_RECIPIENTS
            )
            for change in changes:
                change.notified = True
                change.suppressed_reason = reason
            log.info("no_recipients_assigned", hotels=sorted(hotel_ids), reason=reason)
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
        # Read once for the whole sweep. Twice -- once to pick the numbers and
        # once to label them -- is two reads of a row somebody may be flipping
        # on the Settings page right now, and the two could disagree inside a
        # single message.
        with_tax = monitoring_service.alert_prices_with_tax()
        # Read once per sweep, like the tax switch beside it: two reads of a
        # row somebody may be flipping right now could disagree inside one
        # dispatch, sending to a channel the same run had already skipped.
        email_ok = monitoring_service.email_alerts_enabled()
        if not email_ok:
            log.info("email_alerts_switched_off")
        lines_by_change = _render_lines(session, changes, hotels, with_tax)

        batches = group_for_digest(facts, assignments)

        # Which changes reached a live assignment at all, and which of those
        # cleared somebody's threshold. The difference between the two is the
        # difference between "reactivate that person" and "lower that
        # threshold", so they are tracked apart.
        evaluated: set[int] = set()
        reached: set[int] = set()

        for (recipient_id, hotel_id), batch_ids in batches.items():
            recipient = recipients.get(recipient_id)
            link = links.get((hotel_id, recipient_id))
            if recipient is None or link is None or not link.is_active:
                continue
            evaluated.update(batch_ids)

            kept = [
                cid
                for cid in batch_ids
                if passes_recipient_threshold(
                    _facts_for(by_id[cid]), link.min_delta_abs, link.min_delta_pct
                )
            ]
            if not kept:
                continue
            # Counted before the provider is involved: a notification ROW now
            # exists for these, whether it goes out immediately, is held for
            # quiet hours, or was already written by an earlier attempt.
            reached.update(kept)

            message = render_digest(
                hotels[hotel_id].name,
                [lines_by_change[cid] for cid in sorted(kept)],
                when=now,
                with_tax=with_tax,
            )

            for channel in _channels_in_use(link.channels or ["email"], email_ok):
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
            if change.id in reached:
                change.suppressed_reason = None
            elif change.id not in evaluated:
                change.suppressed_reason = SUPPRESSED_RECIPIENT_INACTIVE
            else:
                change.suppressed_reason = SUPPRESSED_BELOW_THRESHOLD

    for notification_id in created:
        send_notification.apply_async(args=[notification_id], queue="notify")

    log.info("notifications_queued", count=len(created), changes=len(change_ids))
    return {"notifications": len(created)}





@shared_task(name="notify.market_summary", ignore_result=True)
def market_summary() -> dict[str, int]:
    """Every N hours: how many rooms moved, which ones, and where that leaves us.

    Ticks every few minutes and does nothing most of the time. What it sends is
    the LAST CLOSED WINDOW -- with a two-hour interval and a tick at 14:03, the
    window is 12:00 to 14:00 -- so a window is only ever reported once it is
    complete, and never half-full.

    WHY CLOCK-ALIGNED SLOTS AND NOT "TWO HOURS SINCE THE LAST ONE"
    ==============================================================
    A watermark -- send, remember when, send again two hours later -- needs
    somewhere to remember it, and the obvious place is the notifications table.
    But an empty window sends nothing, so there would be no row to remember it
    by, and the window would go on growing until something finally moved: the
    reader would be told "9 rooms changed in the last 14 hours" on a Tuesday
    morning because the market was quiet overnight. Keeping the watermark
    somewhere else makes a worker that died mid-window skip one.

    Slots aligned to midnight in the deployment timezone need no memory at all.
    Every tick computes the same window from the clock, and the dedupe key IS
    the window, so the unique index turns however many ticks land in one slot
    into exactly one message. A worker down for six hours comes back and sends
    the current slot, having lost the ones it slept through -- which is the
    honest outcome, because nobody was there to read them and the movement is
    all still on the dashboard.

    The window named is the one actually covered, so the short final slot of a
    day that the interval does not divide evenly says so rather than rounding.
    """
    interval = monitoring_service.summary_interval_hours()
    if interval <= 0:
        return {"notifications": 0}

    now = datetime.now(UTC)
    window_start, window_end = _last_closed_window(now, interval)
    created: list[int] = []

    with sync_session() as session:
        changes = session.execute(
            select(PriceChange).where(
                PriceChange.changed_at >= window_start,
                PriceChange.changed_at < window_end,
                # PRICE MOVES ONLY. A room selling out and coming back is not
                # a rate the reader can act on, and it crowds out the ones
                # that are: one real window carried fifteen availability
                # notices against three price lines, so the message was
                # mostly a room's inventory flickering.
                #
                # The per-change alert still announces a sell-out the moment
                # it happens, which is when that news is worth having. This
                # is the digest somebody opens to set tomorrow's rate.
                PriceChange.direction.in_(_PRICE_MOVE_DIRECTIONS),
            )
        ).scalars().all()
        if not changes:
            # Nothing moved. Deliberately silent: a message saying so every two
            # hours around the clock is a paid WhatsApp that teaches its reader
            # to ignore the number.
            return {"notifications": 0}

        hotel_ids = {c.hotel_id for c in changes}
        assignments = _assignments_for(session, hotel_ids)
        if not assignments:
            return {"notifications": 0}

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
            for h in session.execute(
                select(Hotel).where(Hotel.id.in_(hotel_ids))
            ).scalars()
        }
        with_tax = monitoring_service.alert_prices_with_tax()
        # Read once per sweep, like the tax switch beside it: two reads of a
        # row somebody may be flipping right now could disagree inside one
        # dispatch, sending to a channel the same run had already skipped.
        email_ok = monitoring_service.email_alerts_enabled()
        if not email_ok:
            log.info("email_alerts_switched_off")
        lines_by_change = _render_lines(session, changes, hotels, with_tax)
        facts_by_id = {c.id: _facts_for(c) for c in changes}

        # recipient -> the changes that earned a place in their summary, and
        # the channels to send it on.
        #
        # Grouped by PERSON, not by hotel. The message answers "how busy was
        # the last two hours", and that question is about everything they
        # watch: four properties moving produces one message saying four rooms
        # changed, not four messages each saying one did. The per-change alert
        # is the one that speaks per hotel, and it still does.
        pending: dict[int, list[int]] = {}
        channels: dict[int, list[str]] = {}

        batches = group_for_digest(list(facts_by_id.values()), assignments)
        for (recipient_id, hotel_id), batch_ids in batches.items():
            recipient = recipients.get(recipient_id)
            link = links.get((hotel_id, recipient_id))
            if recipient is None or link is None or not link.is_active:
                continue
            # The same threshold the immediate alert applied. A summary that
            # counted moves the reader was never told about would disagree with
            # their own inbox, and the inbox is what they trust.
            kept = [
                cid
                for cid in batch_ids
                if passes_recipient_threshold(
                    facts_by_id[cid], link.min_delta_abs, link.min_delta_pct
                )
            ]
            if not kept:
                continue

            pending.setdefault(recipient_id, []).extend(kept)
            # Unioned in order, not replaced. The same person can hold email on
            # one property and email+WhatsApp on another, and the one message
            # covering both has to go everywhere either assignment asked for --
            # dropping a channel here would silence a number that was reaching
            # them yesterday.
            bucket = channels.setdefault(recipient_id, [])
            for channel in _channels_in_use(link.channels or ["email"], email_ok):
                if channel not in bucket:
                    bucket.append(channel)

        hours = _window_hours(window_start, window_end)

        for recipient_id, change_ids in pending.items():
            recipient = recipients[recipient_id]
            ids = sorted(set(change_ids))
            message = render_summary(
                [lines_by_change[cid] for cid in ids if cid in lines_by_change],
                window_hours=hours,
                when=now,
                param_count=_comparison_param_count(),
                with_tax=with_tax,
            )
            for channel in channels[recipient_id]:
                notification_id = _create_notification(
                    session,
                    recipient=recipient,
                    # No hotel. The message is about a window across everything
                    # this person watches, and filing it under whichever
                    # property moved first would put one property's name on a
                    # count that is not only about it.
                    hotel_id=None,
                    channel=channel,
                    change_ids=ids,
                    subject=message.subject,
                    body=message.text,
                    now=now,
                    kind=MARKET_COMPARISON,
                    dedupe=summary_dedupe_key(recipient.id, channel, window_start),
                )
                if notification_id is not None:
                    created.append(notification_id)

        change_count = len(changes)

    for notification_id in created:
        send_notification.apply_async(args=[notification_id], queue="notify")

    log.info(
        "market_summary_queued",
        count=len(created),
        changes=change_count,
        window_start=window_start.isoformat(),
        hours=hours,
    )
    return {"notifications": len(created)}


def _last_closed_window(now: datetime, interval: int) -> tuple[datetime, datetime]:
    """The most recent complete slot, aligned to midnight in the local zone.

    Aligned to midnight rather than to the epoch so the slots are ones a person
    would name: an interval of two puts a message at 8, 10 and 12, rather than
    at 7:38 because that is when the worker happened to start.

    An interval that does not divide 24 leaves a short final slot before
    midnight. It is reported honestly -- the message names the hours it
    actually covers, not the configured interval. See ``_moved_headline``.
    """
    zone = _zone(get_settings().timezone)
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)

    elapsed = int((local - midnight).total_seconds() // 3600)
    boundary = (elapsed // interval) * interval

    if boundary == 0:
        # Before today's first boundary, so the last closed slot is yesterday's
        # final one. Measured back from midnight rather than assumed to be a
        # whole interval long: when the interval does not divide 24 that slot
        # is short, and claiming otherwise would name hours it did not cover.
        whole = (24 // interval) * interval
        start = midnight - timedelta(hours=24 - whole or interval)
        return start.astimezone(UTC), midnight.astimezone(UTC)

    end = midnight + timedelta(hours=boundary)
    return (end - timedelta(hours=interval)).astimezone(UTC), end.astimezone(UTC)


def _window_hours(start: datetime, end: datetime) -> int:
    """How long the window actually was, in whole hours, at least one."""
    return max(1, round((end - start).total_seconds() / 3600))


def _create_notification(
    session: Session,
    *,
    recipient: Recipient,
    hotel_id: int | None,
    channel: str,
    change_ids: list[int],
    subject: str,
    body: str,
    now: datetime,
    kind: str = PRICE_CHANGE,
    dedupe: str | None = None,
) -> int | None:
    """Insert the notification row, honouring quiet hours and the dedupe key.

    Returns ``None`` when the row already existed (a retry) or when the
    message is being held for later — in both cases there is nothing to send
    right now.

    ``dedupe`` overrides the key for messages that are not about a set of price
    changes. An ops alert has no change ids, so every one of them would hash to
    the same key and only the first would ever be written.
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
    # An alert number is exempt: it was added to be told immediately, at any
    # hour. Held messages are the normal case and this is the exception, so the
    # flag is checked here rather than folded into in_quiet_hours().
    if not recipient.bypass_throttle and in_quiet_hours(
        local_now.time(), quiet_start, quiet_end
    ):
        scheduled_for = release_time(now, quiet_end, recipient.timezone)

    notification = Notification(
        recipient_id=recipient.id,
        hotel_id=hotel_id,
        channel=channel,
        provider=provider.provider_name,
        kind=kind,
        dedupe_key=dedupe or dedupe_key(recipient.id, channel, change_ids),
        price_change_ids=change_ids,
        subject=subject[:300],
        body_rendered=body,
        status=NotificationStatus.QUEUED,
        created_at=now,
        scheduled_for=scheduled_for,
    )
    # Read BEFORE the insert is attempted. A failed flush rolls the transaction
    # back, and every attribute on every instance in the session is expired --
    # so reading recipient.id in the except block below fires a lazy load
    # against a transaction that can no longer run one, and PendingRollbackError
    # replaces the duplicate we were handling.
    recipient_id = recipient.id

    try:
        # BOTH the add and the flush inside the savepoint. Session.begin_nested()
        # flushes anything already pending as it takes its snapshot, so an add()
        # placed above this line is written on the OUTER transaction and the
        # savepoint never covers it -- a duplicate then poisons the surrounding
        # transaction instead of being caught here, and every later recipient in
        # the same run is lost with it.
        #
        # Rarely reached from dispatch_changes, which only sees a duplicate on a
        # Celery retry. The market summary reaches it on every tick: the task
        # keeps no state and re-offers the same window until one message is
        # written, so this IS the mechanism that stops a second send.
        with session.begin_nested():
            session.add(notification)
            session.flush()
    except IntegrityError:
        log.info("notification_deduplicated", recipient_id=recipient_id, channel=channel)
        return None

    if scheduled_for is not None:
        log.info(
            "notification_held_for_quiet_hours",
            recipient_id=recipient_id,
            release_at=scheduled_for.isoformat(),
        )
        return None

    return notification.id


def notify_ops(
    session: Session,
    *,
    subject: str,
    body: str,
    token: str,
    now: datetime | None = None,
) -> list[int]:
    """Tell the ops contacts something about the system itself.

    Price alerts answer "what did a hotel do?"; these answer "is this thing
    still working?". They share the notifications table so an ops alert has the
    same delivery history, retry behaviour and audit trail as everything else —
    a separate side-channel would be the one path nobody could see had failed.

    ``token`` decides how often the same problem may interrupt someone; see
    :func:`app.notifications.digest.ops_dedupe_key`.

    Returns the notification ids to enqueue. The caller enqueues them *after*
    its transaction commits, or the worker can pick up a row that does not
    exist yet.
    """
    now = now or datetime.now(UTC)

    contacts = session.execute(
        select(Recipient).where(
            Recipient.is_active.is_(True),
            Recipient.receives_ops_alerts.is_(True),
        )
    ).scalars().all()

    if not contacts:
        # Deliberately a warning. A deployment with no ops contact cannot be
        # told that it has stopped working, which is worth saying out loud
        # every time rather than once in a setup guide.
        log.warning("no_ops_contacts_configured", subject=subject)
        return []

    available = set(registry.available_channels())
    created: list[int] = []

    for recipient in contacts:
        # Ops alerts are email-only, and a contact with only a phone number is
        # skipped rather than reached over WhatsApp.
        #
        # This looks like a downgrade and is the opposite. A business-initiated
        # WhatsApp message must use an approved template, and the only approved
        # template takes seven positional parameters describing a price move --
        # hotel, room, old, new, delta, dates, time. An ops alert has none of
        # them, so the send is rejected with 132000, which is permanent and
        # therefore never retried. The result was that "your monitoring has
        # gone quiet" was the one message that could not be delivered: the
        # deployment learned it had stopped working by not being told.
        #
        # Carrying these on WhatsApp needs a SECOND approved template and a
        # second approval cycle. Until that exists, a loud warning here beats a
        # send that is guaranteed to fail silently.
        #
        # Email also carries a list of hostnames and timestamps far better than
        # a chat bubble does, which is why it was already preferred.
        if recipient.email and "email" in available:
            channel = "email"
        else:
            log.warning(
                "ops_contact_unreachable",
                recipient_id=recipient.id,
                has_email=bool(recipient.email),
                has_phone=bool(recipient.phone_e164),
            )
            continue

        notification_id = _create_notification(
            session,
            recipient=recipient,
            hotel_id=None,
            channel=channel,
            change_ids=[],
            subject=subject,
            body=body,
            now=now,
            dedupe=ops_dedupe_key(recipient.id, channel, token),
        )
        if notification_id is not None:
            created.append(notification_id)

    return created


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
        if not recipient.bypass_throttle:
            remaining = recipient_quota_remaining(
                recipient.id, settings.recipient_max_msgs_per_hour
            )
            if remaining <= 0:
                # Over the hourly cap. Held rather than dropped, and released
                # by the same sweep that handles quiet hours.
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
def _all_hotel_recipient_ids(session: Session) -> list[int]:
    """Recipients that follow every hotel, present and future.

    These are the WhatsApp alert numbers from the Alerts page. Resolved on
    every dispatch rather than written into hotel_recipients when a hotel is
    created: hotels arrive from the API, from discovery and from scripts, and a
    backfill missed on any one of those paths produces a hotel that alerts
    nobody -- which is indistinguishable, on every screen, from a hotel whose
    price never moved.
    """
    return list(
        session.execute(
            select(Recipient.id).where(
                Recipient.alerts_all_hotels.is_(True), Recipient.is_active.is_(True)
            )
        ).scalars()
    )


def _assignments_for(session: Session, hotel_ids: set[int]) -> dict[int, list[int]]:
    rows = session.execute(
        select(HotelRecipient.hotel_id, HotelRecipient.recipient_id).where(
            HotelRecipient.hotel_id.in_(hotel_ids), HotelRecipient.is_active.is_(True)
        )
    ).all()
    assignments: dict[int, list[int]] = {}
    for hotel_id, recipient_id in rows:
        assignments.setdefault(hotel_id, []).append(recipient_id)

    # Union rather than append: an alert number that ALSO has a real
    # assignment to this hotel must appear once, or it is batched twice and
    # the person is messaged twice about the same move.
    for recipient_id in _all_hotel_recipient_ids(session):
        for hotel_id in hotel_ids:
            bucket = assignments.setdefault(hotel_id, [])
            if recipient_id not in bucket:
                bucket.append(recipient_id)
    return assignments


def _any_assignment_exists(session: Session, hotel_ids: set[int]) -> bool:
    """Is there an assignment here at all, active or not?

    Only asked once the active ones have already come back empty, so this runs
    on the failure path and never on the ordinary one.
    """
    return session.execute(
        select(HotelRecipient.id).where(HotelRecipient.hotel_id.in_(hotel_ids)).limit(1)
    ).first() is not None


def _links_by_pair(session: Session, hotel_ids: set[int]) -> dict[tuple[int, int], HotelRecipient]:
    """The assignment behind each (hotel, recipient) pair.

    ``dispatch_changes`` skips any pair with no link, because the link carries
    the channels and the thresholds. An alert number has no row for a hotel
    nobody assigned it to, so one is synthesised here -- unsaved, never added
    to the session, and used only to answer "which channels, what threshold"
    for this dispatch.

    WHATSAPP IS ADDED TO A REAL ROW, NOT BLOCKED BY IT.
    ===================================================
    Letting a real row win outright looked like the careful choice and quietly
    broke the feature's whole promise. An alert number is usually somebody who
    is ALREADY a recipient: one number added on the Alerts page covered ten
    hotels, and reached WhatsApp on exactly one of them -- the only hotel with
    no prior assignment. The other nine had ``channels=['email']`` rows from
    before, which suppressed the synthesised link entirely. The panel said
    "every price change, on every hotel" and delivered it for one in ten, with
    nothing anywhere reporting a problem.

    So the channel is unioned in. Everything else on the row is left exactly as
    it was: its threshold still decides which changes are worth sending, and
    email keeps behaving as it did, because adding a phone number is not a
    statement about anybody's mail.

    A DEACTIVATED assignment is treated as no assignment at all, not as a
    veto. Switching one hotel off for somebody predates their being an alert
    number and cannot express "...but still WhatsApp me about it", whereas the
    flag says exactly that. The synthesised link that results is WhatsApp-only,
    so an assignment switched off does not quietly resume sending email.

    Nothing here is added to the session. These objects answer one dispatch;
    persisting them would freeze today's hotel list into the database and undo
    the future-hotel guarantee the flag exists for.
    """
    rows = session.execute(
        select(HotelRecipient).where(HotelRecipient.hotel_id.in_(hotel_ids))
    ).scalars().all()
    links = {(link.hotel_id, link.recipient_id): link for link in rows}

    for recipient_id in _all_hotel_recipient_ids(session):
        for hotel_id in hotel_ids:
            existing = links.get((hotel_id, recipient_id))

            if existing is not None and existing.is_active:
                channels = list(existing.channels or [])
                if "whatsapp" in channels:
                    continue
                # A COPY, never a mutation. ``existing`` is attached to the
                # session, so appending to its channels would mark it dirty and
                # rewrite the row on the next commit -- turning a decision made
                # for one dispatch into a permanent, invisible edit to the
                # operator's own configuration.
                links[(hotel_id, recipient_id)] = HotelRecipient(
                    hotel_id=hotel_id,
                    recipient_id=recipient_id,
                    channels=[*channels, "whatsapp"],
                    min_delta_abs=existing.min_delta_abs,
                    min_delta_pct=existing.min_delta_pct,
                    is_active=True,
                )
                continue

            links[(hotel_id, recipient_id)] = HotelRecipient(
                hotel_id=hotel_id,
                recipient_id=recipient_id,
                # WhatsApp only. These are phone numbers collected on the
                # Alerts page; several have no email address at all, and
                # defaulting to email would queue a message with nowhere to go.
                channels=["whatsapp"],
                # No threshold: the point of an alert number is every move.
                min_delta_abs=None,
                min_delta_pct=None,
                is_active=True,
            )
    return links


def _render_lines(
    session: Session,
    changes: list[PriceChange],
    hotels: dict[int, Hotel],
    with_tax: bool | None = None,
) -> dict[int, ChangeLine]:
    """Join each change to the room and stay it belongs to.

    One query for the series rows rather than one per change: a weekend-wide
    reprice can carry a hundred changes, and a hundred round trips inside the
    notification path is how alerts start arriving minutes late.

    THE NUMBERS ARE CHOSEN HERE, NOT IN THE RENDERER
    ================================================
    A change stores two prices on the comparison basis and, beside them, what
    each side was made of. Whether the message quotes ₹9,000 or ₹10,620 is the
    Settings switch's decision -- the same one every screen obeys -- and
    honouring it needs a database read, which the renderer deliberately cannot
    do. So it happens once, here, for both the per-hotel digest and the market
    summary, and the chosen basis is handed to the renderer alongside the
    numbers so the message can say which it is.

    The difference is recomputed from the two figures actually printed rather
    than carried from the row: see ``price_display.displayed_move``.

    ``with_tax=None`` reads the switch. It is a parameter at all so a caller
    that already read it -- and every one of them does, to pass it on to the
    renderer -- cannot render on one value and label on another.
    """
    if with_tax is None:
        with_tax = monitoring_service.alert_prices_with_tax()
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
        move = displayed_move(
            # ``current_price`` is the last-resort figure the fallback drops to
            # when no component was recorded -- a change written before the
            # components existed, or by a source that publishes one bare
            # number. It is the price this alert has always quoted, and the
            # switch must not blank it. See services/price_display.py.
            Components(
                current_price=change.old_price,
                last_price_exclusive=change.old_price_exclusive,
                last_taxes_fees=change.old_taxes_fees,
                last_price_inclusive=change.old_price_inclusive,
            ),
            Components(
                current_price=change.new_price,
                last_price_exclusive=change.new_price_exclusive,
                last_taxes_fees=change.new_taxes_fees,
                last_price_inclusive=change.new_price_inclusive,
            ),
            with_tax,
        )
        lines[change.id] = ChangeLine(
            hotel_name=hotels[change.hotel_id].name if change.hotel_id in hotels else "Unknown",
            room_name=room.name if room else "(room)",
            old_price=move.old,
            new_price=move.new,
            delta=move.delta,
            delta_pct=move.delta_pct,
            basis_note=move.note,
            currency=change.currency,
            direction=str(change.direction),
            check_in=entry.check_in.isoformat() if entry else "",
            check_out=entry.check_out.isoformat() if entry else "",
            meal_plan=entry.meal_plan if entry else None,
            is_overnight=change.previous_offer_key is not None,
        )
    return lines


def _rebuild_message(session: Session, notification: Notification, subject: str, body: str):
    """Reconstruct the rendered message for a notification about to be sent.

    Rebuilt from the change ids rather than stored per channel, so a template
    fix applies to a message that has been sitting in a quiet-hours hold since
    last night.

    ``notification.kind`` decides which message is rebuilt, and it is read off
    the row rather than from the setting. A summary queued at 11 PM must be
    released at 7 AM as a summary even if somebody set the interval to zero in
    between -- and it must reach WhatsApp on the template it was queued for,
    because the other one would take its parameters and mean something else.
    """
    changes = session.execute(
        select(PriceChange).where(PriceChange.id.in_(notification.price_change_ids))
    ).scalars().all()
    if not changes:
        return RenderedMessage(
            subject=subject, text=body, html=None, kind=notification.kind
        )

    hotels = {
        h.id: h
        for h in session.execute(
            select(Hotel).where(Hotel.id.in_({c.hotel_id for c in changes}))
        ).scalars()
    }

    if notification.kind == MARKET_COMPARISON:
        return _rebuild_summary(session, notification, changes, hotels, subject, body)

    # The switch as it stands NOW, not as it stood when this was queued.
    #
    # Deliberately unlike ``notification.kind``, which is read off the row: the
    # kind decides which approved WhatsApp template carries the message and
    # getting that wrong sends the wrong meaning down the wrong slots. This
    # decides only which of two true numbers to print, and a message released
    # at 7 AM should read the way the dashboard reads at 7 AM -- that is the
    # whole point of one switch for both.
    with_tax = monitoring_service.alert_prices_with_tax()
    lines = _render_lines(session, changes, hotels, with_tax)
    hotel_name = hotels[changes[0].hotel_id].name if changes[0].hotel_id in hotels else "Hotel"
    return render_digest(
        hotel_name,
        [lines[c.id] for c in changes],
        when=notification.created_at,
        with_tax=with_tax,
    )


def _rebuild_summary(
    session: Session,
    notification: Notification,
    changes: list[PriceChange],
    hotels: dict[int, Hotel],
    subject: str,
    body: str,
):
    """The market summary again, for a message about to be sent.

    Replayed from the row rather than recounted. Those changes happened inside
    a window that closed hours ago and nothing can alter them now, so a summary
    released at 7 AM still says what the 11 PM window contained -- not what has
    moved since.

    Falls back to the stored text when the changes are gone. The stored body is
    the audit record of what was said when this was queued, so email still
    carries something true; WhatsApp will refuse it for want of template
    parameters, which is the honest outcome, because there is nothing to put in
    them.
    """
    with_tax = monitoring_service.alert_prices_with_tax()
    lines = _render_lines(session, changes, hotels, with_tax)
    moved = [lines[c.id] for c in sorted(changes, key=lambda c: c.id) if c.id in lines]
    if not moved:
        log.warning("market_summary_rebuild_empty", notification_id=notification.id)
        return RenderedMessage(
            subject=subject, text=body, html=None, kind=MARKET_COMPARISON
        )

    # The window this row reported, recovered from when it was written. The
    # task queues a summary on the first tick AFTER a window closes, so the
    # last closed window at ``created_at`` is the one that was reported.
    interval = monitoring_service.summary_interval_hours() or 1
    window_start, window_end = _last_closed_window(notification.created_at, interval)

    return render_summary(
        moved,
        window_hours=_window_hours(window_start, window_end),
        when=notification.created_at,
        param_count=_comparison_param_count(),
        with_tax=with_tax,
    )


def _comparison_param_count() -> int:
    """How many body variables the approved market-summary template has.

    Read here rather than in the renderer, which stays pure. Two of them are
    fixed and every one above those carries more of the moves, so this number
    decides how many changed rooms fit on WhatsApp -- email and the stored text
    always carry all of them.

    Floored at the minimum: a count below it leaves no slot for a move at all,
    and a summary with nothing in it is worse than the mismatch error the
    provider would otherwise raise.
    """
    configured = get_settings().whatsapp_comparison_template_params
    return max(WHATSAPP_COMPARISON_MIN_PARAMS, int(configured))


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
