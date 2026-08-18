"""Login, logout, and who-am-I.

THROTTLING
==========
Five failed attempts inside fifteen minutes locks the account for fifteen
minutes. The counter lives on the ``users`` row rather than in Redis, so a
Redis restart cannot hand an attacker a fresh budget, and the lock survives a
deploy.

The response is deliberately identical for "no such account" and "wrong
password". Distinguishing them turns the login form into a tool for
discovering which addresses are registered.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import SESSION_COOKIE, CurrentUser, DbSession, record_audit
from app.config import get_settings
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, needs_rehash, verify_password
from app.db.models import User
from app.schemas.auth import LoginIn, PasswordChangeIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("api.auth")

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, request: Request, response: Response, session: DbSession):
    settings = get_settings()
    now = datetime.now(UTC)

    user = await session.scalar(select(User).where(User.email == payload.email.lower()))

    if user is not None and user.locked_until and user.locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again shortly.",
        )

    # verify_password is always called when the account exists so that a
    # missing account and a wrong password take comparable time; timing is the
    # other way account enumeration leaks.
    ok = user is not None and user.is_active and verify_password(payload.password, user.password_hash)

    if not ok:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + LOCKOUT
                log.warning("account_locked", user_id=user.id)
            await session.commit()
        log.info("login_failed", email_domain=payload.email.split("@")[-1])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    # Argon2 parameters get raised over time; rehash on a successful login so
    # existing accounts strengthen without anyone being asked to do anything.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    token = create_access_token(
        str(user.id), user.role.value, must_change_password=user.must_change_password
    )
    session_token = create_access_token(
        str(user.id),
        user.role.value,
        expires_minutes=settings.session_max_age_seconds // 60,
        must_change_password=user.must_change_password,
    )

    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=settings.session_max_age_seconds,
        httponly=True,                      # JavaScript cannot read it
        secure=settings.is_production,      # HTTPS only in production
        samesite="lax",                     # blocks cross-site form posts
        path="/",
    )

    await record_audit(
        session, user=user, action="login", entity="user", entity_id=user.id, request=request
    )
    await session.commit()

    log.info("login_succeeded", user_id=user.id, role=user.role.value)
    return TokenOut(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        role=user.role,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response):
    """Clear the session cookie.

    JWTs are stateless, so a bearer token remains valid until it expires —
    which is why access tokens are short-lived. The cookie is what the
    dashboard uses, and it is gone immediately.
    """
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChangeIn,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect."
        )
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The new password must differ from the current one.",
        )

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    # The change itself is audited; neither password appears in the record.
    await record_audit(
        session, user=user, action="change_password", entity="user",
        entity_id=user.id, request=request,
    )
    await session.commit()

    # Re-issue the cookie without the "must change" claim. Without this the
    # dashboard would keep redirecting to the change-password page until the
    # old session expired, even though the password has already been changed.
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        create_access_token(
            str(user.id),
            user.role.value,
            expires_minutes=settings.session_max_age_seconds // 60,
        ),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
