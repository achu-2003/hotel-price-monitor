"""FastAPI dependencies: database session, authentication, RBAC, audit.

**Authorisation is a dependency, never a template condition.** A hidden button
is not a permission check — the endpoint is still there and still reachable
with curl. Every mutating route depends on :func:`require_admin`, so a viewer
gets a 403 from the API itself regardless of what the dashboard renders.

Two credentials are accepted, resolved in this order:

1. ``Authorization: Bearer <jwt>`` — for scripts and integrations
2. a signed session cookie — for the dashboard, HttpOnly so JavaScript on the
   page cannot read it even if something manages to inject a script
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import get_logger
from app.core.redaction import scrub
from app.core.security import decode_token
from app.db.models import AuditLog, User, UserRole
from app.db.session import get_db

log = get_logger("api.deps")

SESSION_COOKIE = "hpm_session"


async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_db():
        yield session


DbSession = Annotated[AsyncSession, Depends(db_session)]


async def current_user(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    hpm_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    """Resolve the caller, or raise 401.

    The token is verified before the database is touched, so an invalid token
    costs one signature check rather than a query — which is what stops a
    credential-stuffing attempt from also being a database load test.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif hpm_session:
        token = hpm_session

    if not token:
        raise _unauthorised("Not authenticated")

    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorised("Session expired") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorised("Invalid credentials") from exc

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError) as exc:
        raise _unauthorised("Invalid credentials") from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        # Checked on every request rather than trusted from the token: a
        # deactivated account must lose access immediately, not in fifteen
        # minutes when its token happens to expire.
        raise _unauthorised("Account is not active")

    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    """Mutation requires an admin. Viewers get a 403, not a hidden button."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires the admin role.",
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def record_audit(
    session: AsyncSession,
    *,
    user: User | None,
    action: str,
    entity: str,
    entity_id: str | int | None,
    before: dict | None = None,
    after: dict | None = None,
    request: Request | None = None,
) -> None:
    """Append to the configuration audit trail.

    Both payloads are scrubbed: a credential edit should record that it
    happened and who did it, never what the value was.
    """
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=_jsonable(scrub(before)) if before else None,
            after=_jsonable(scrub(after)) if after else None,
            ip_address=_client_ip(request),
            user_agent=(request.headers.get("user-agent") if request else None),
            at=datetime.now(UTC),
        )
    )


def _jsonable(value):
    """Coerce a scrubbed payload into something JSONB will accept.

    A "before" snapshot is built straight from ORM columns, so it routinely
    contains ``datetime``, ``date``, ``Decimal`` and enum values. Handing those
    to a JSONB column raises at flush time — and because the audit write is
    part of the same transaction as the change it records, that failure would
    roll back the operation the user actually asked for.
    """
    from datetime import date, datetime, time
    from decimal import Decimal
    from enum import Enum

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _client_ip(request: Request | None) -> str | None:
    """The caller's address, honouring one proxy hop.

    Only the FIRST entry of ``X-Forwarded-For`` is used and only because Caddy
    sits in front of this app. Trusting the whole chain would let a caller
    write any address they liked into the audit log.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_object_or_404(session: AsyncSession, model, object_id, label: str):
    """Fetch by primary key or raise a 404 that names what was missing."""
    obj = await session.get(model, object_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} {object_id} does not exist.",
        )
    return obj


async def owned_hotel_or_404(session: AsyncSession, hotel_id: int, user: User):
    """Fetch a hotel this account owns, or 404.

    404 rather than 403 when the hotel exists but belongs to someone else.
    A 403 would confirm that hotel 41 is a real property somebody is watching,
    which is the same enumeration leak the login endpoint goes out of its way
    to avoid — and here the thing being enumerated is a competitor set.
    """
    from app.db.models import Hotel  # local: app.db.models imports nothing here

    hotel = await session.get(Hotel, hotel_id)
    if hotel is None or hotel.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hotel {hotel_id} does not exist.",
        )
    return hotel


async def unique_or_409(session: AsyncSession, model, label: str, **filters):
    """Reject a duplicate before the database does.

    A caught IntegrityError would work, but it aborts the transaction and the
    resulting message is a Postgres constraint name rather than anything an
    operator can act on.
    """
    existing = await session.scalar(select(model).filter_by(**filters))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{label} already exists: {filters}",
        )


def settings_dep():
    return get_settings()
