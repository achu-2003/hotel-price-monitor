"""The link at the bottom of an alert, and what it opens.

An alert answers "what moved". The reader's next question is always "against
whom", and the message deliberately does not answer it -- see the note in
``render_summary`` about what a comparison grid does to four price lines. This
is the bridge: a short URL that opens the comparison for the night the message
was about, readable by somebody who has no dashboard account.

WHAT A LINK IS AND IS NOT
=========================
It is a row naming an owner, a night and an occupancy. It is not a login, and
it cannot become one: no user id travels in the URL, the page it opens has no
navigation, and every query behind that page filters by the owner on the row --
the same filter the signed-in page applies to the logged-in user.

It is a bearer link. Anyone the message reaches, or is forwarded to, can open
it until it expires. That is the deliberate trade for recipients who are phone
numbers rather than accounts, and it is why the expiry is not optional.
"""
from __future__ import annotations

import secrets
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ComparisonLink, PriceChange, PriceSeries

log = structlog.get_logger(__name__)

#: Twelve random bytes, base64url. Sixteen characters, no comma -- a parameter
#: containing one is split by the My Dreams reseller and arrives as two.
_TOKEN_BYTES = 12


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def public_url(token: str) -> str | None:
    """The address to print, or ``None`` when this deployment has no public one.

    NONE IS A REAL ANSWER AND MUST NOT BE PAPERED OVER. A worker has no
    request, so there is no Host header to fall back on, and a message carrying
    ``http://127.0.0.1:8000`` is worse than one carrying nothing: every reader
    taps a dead link, and the feature looks like it is working.
    """
    base = (get_settings().public_base_url or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/c/{token}"


def stay_of(session: Session, changes: list[PriceChange]) -> tuple[date, date, int] | None:
    """The night these moves were about, and for how many people.

    The commonest stay among the series behind them, not the earliest. A
    summary can carry a weekend reprice alongside one hotel's next-Tuesday
    move, and the link should open on the night most of the message is talking
    about rather than whichever sorts first.

    Ties break towards the earlier night, which is the one closest to being
    actionable today.
    """
    if not changes:
        return None
    offer_keys = {c.offer_key for c in changes}
    rows = session.execute(
        select(PriceSeries.check_in, PriceSeries.check_out, PriceSeries.adults)
        .where(PriceSeries.offer_key.in_(offer_keys))
    ).all()
    if not rows:
        return None
    counts = Counter((r.check_in, r.check_out, r.adults) for r in rows)
    best = max(counts.items(), key=lambda kv: (kv[1], -kv[0][0].toordinal()))
    return best[0]


def ensure_link(
    session: Session,
    *,
    owner_user_id: int,
    check_in: date,
    check_out: date,
    adults: int,
    baseline_hotel_id: int | None = None,
    now: datetime | None = None,
) -> ComparisonLink:
    """The link for this scope, reused if it exists.

    ONE ROW PER SCOPE, NOT ONE PER MESSAGE. Four alert numbers times a
    two-hourly summary times ten hotels is a new row every few minutes, every
    one of them opening the identical page. Reuse also keeps the URL stable, so
    a reader who kept this morning's message still lands somewhere current.

    Reuse EXTENDS the expiry. A scope that is still being alerted about is one
    somebody may still open; letting it lapse on the age of the first message
    that mentioned it would kill a link the latest message just printed.

    Does not commit -- the caller owns the transaction the notification is
    being written in, and a link committed beside a message that then failed to
    build is a row pointing at nothing.
    """
    now = now or datetime.now(UTC)
    expires = now + timedelta(days=get_settings().comparison_link_days)

    link = session.scalar(
        select(ComparisonLink).where(
            ComparisonLink.owner_user_id == owner_user_id,
            ComparisonLink.check_in == check_in,
            ComparisonLink.check_out == check_out,
            ComparisonLink.adults == adults,
            ComparisonLink.baseline_hotel_id == baseline_hotel_id,
        )
    )
    if link is not None:
        if link.expires_at < expires:
            link.expires_at = expires
        return link

    link = ComparisonLink(
        token=_new_token(),
        owner_user_id=owner_user_id,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        baseline_hotel_id=baseline_hotel_id,
        expires_at=expires,
    )
    session.add(link)
    session.flush()
    log.info("comparison_link_created", token=link.token, owner_user_id=owner_user_id)
    return link


async def resolve(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> ComparisonLink | None:
    """The link behind a token, or ``None`` if there is not a live one.

    Expiry is checked HERE rather than left to the sweep. The sweep runs daily
    and is a tidy-up; a link that expired an hour ago must already be dead, or
    the expiry is a suggestion.
    """
    now = now or datetime.now(UTC)
    link = await session.scalar(
        select(ComparisonLink).where(ComparisonLink.token == token)
    )
    if link is None or link.expires_at <= now:
        return None
    return link


def sweep_expired(session: Session, *, now: datetime | None = None) -> int:
    """Delete links that have lapsed. Returns how many.

    Deleted rather than flagged: an expired link grants nothing, holds no
    history worth keeping, and the table would otherwise grow by a row per
    night per owner forever.
    """
    now = now or datetime.now(UTC)
    result = session.execute(
        delete(ComparisonLink).where(ComparisonLink.expires_at <= now)
    )
    return int(result.rowcount or 0)
