"""Batching, quiet hours, and idempotency — the rules about *when* to interrupt.

Pure functions, no database and no clock of their own, because these are the
decisions most likely to be wrong in a way nobody notices until someone mutes
the alerts.

THREE PROTECTIONS
=================
**Digest batching.** All of one hotel's changes in a 60-second window become
one message. A market-wide weekend reprice produces a hundred changes in one
cycle; a hundred separate messages at 5:30 PM gets the system muted, after
which no alert reaches anyone at all.

**Quiet hours.** Anything landing between 22:00 and 07:00 local time is held
and released in the morning. A ₹200 move at 3 AM is not worth a phone buzzing.
Note that it is *held*, not dropped — the change is real and still gets told.

**Idempotency.** ``dedupe_key`` hashes (recipient, channel, the exact set of
change ids) and the notifications table has a unique index on it. A Celery
retry after a provider timeout therefore cannot double-send, which matters
because "did that actually go out?" is unanswerable at the provider level.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

DEDUPE_VERSION = "v1"


def dedupe_key(recipient_id: int, channel: str, change_ids: list[int]) -> str:
    """Stable identity for "this exact alert to this exact person".

    Sorted, so the same set of changes arriving in a different order produces
    the same key. Versioned, so a future change to the batching rules does not
    silently suppress messages that the old rules had already keyed.
    """
    payload = f"{DEDUPE_VERSION}|{recipient_id}|{channel}|{','.join(map(str, sorted(change_ids)))}"
    return hashlib.sha256(payload.encode()).hexdigest()


def in_quiet_hours(now_local: time, start: time | None, end: time | None) -> bool:
    """Whether ``now_local`` falls inside the quiet window.

    Handles the wrap past midnight, which is the normal case: 22:00 to 07:00
    is one window, not two, and the naive ``start <= now <= end`` comparison
    would report it as never active.
    """
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= now_local < end
    return now_local >= start or now_local < end


def release_time(
    now: datetime, end: time | None, tz_name: str = "Asia/Kolkata"
) -> datetime | None:
    """When a held message should go out: the next occurrence of ``end``.

    Returned in UTC so it can be compared against a stored timestamp without
    another timezone conversion at the point of use.
    """
    if end is None:
        return None
    zone = ZoneInfo(tz_name)
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), end, tzinfo=zone)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), end, tzinfo=zone)
    return target.astimezone(now.tzinfo or zone)


@dataclass(frozen=True, slots=True)
class ChangeFacts:
    """The parts of a price change that decide whether it is worth sending.

    Kept separate from ``ChangeLine`` (which is about rendering) so the
    filtering rules can be tested without constructing a message.
    """

    change_id: int
    hotel_id: int
    delta: Decimal | None
    delta_pct: Decimal | None
    direction: str


def passes_recipient_threshold(
    facts: ChangeFacts,
    min_delta_abs: Decimal | None,
    min_delta_pct: Decimal | None,
) -> bool:
    """A second, per-person filter on top of the target's own thresholds.

    The same change can be worth telling the manager of the hotel next door
    and not worth telling someone tracking a property across the valley, so
    sensitivity lives on the assignment as well as on the target.

    Availability events always pass: "sold out" has no percentage, and it is
    the kind of thing the assigned person wants regardless.
    """
    if facts.direction in {"became_unavailable", "became_available"}:
        return True
    if min_delta_abs is not None and (facts.delta is None or abs(facts.delta) < min_delta_abs):
        return False
    if min_delta_pct is not None and (
        facts.delta_pct is None or abs(facts.delta_pct) < min_delta_pct
    ):
        return False
    return True


def group_for_digest(
    facts: list[ChangeFacts],
    assignments: dict[int, list[int]],
) -> dict[tuple[int, int], list[int]]:
    """Group changes into one batch per (recipient, hotel).

    Args:
        facts: the changes being dispatched.
        assignments: ``{hotel_id: [recipient_id, ...]}``.

    Grouping by hotel as well as by recipient is deliberate. Someone
    responsible for four properties gets four messages rather than one mixed
    digest, because each one is a different decision they might act on.
    """
    batches: dict[tuple[int, int], list[int]] = defaultdict(list)
    for fact in facts:
        for recipient_id in assignments.get(fact.hotel_id, ()):
            batches[(recipient_id, fact.hotel_id)].append(fact.change_id)
    return dict(batches)


def is_weekend_stay(check_in: date) -> bool:
    """Friday or Saturday night.

    Not used for filtering — weekend rates simply move more, and flagging them
    keeps that visible when someone is reading a week of history.
    """
    return check_in.weekday() in (4, 5)
