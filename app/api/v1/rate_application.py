"""The application an owner's rates are changed in.

One resource per owner, addressed without an id: ``GET`` says what is set,
``PUT`` sets it, ``DELETE`` forgets it. Owner-scoped like every hotel query --
the row is looked up by the caller, never by a number the caller sent, so
there is no way to read or overwrite somebody else's login.

THE PASSWORD GOES IN AND NEVER COMES OUT. It is sealed on the way in and the
response model has no field for it; the audit row records that it changed and
who changed it, and ``record_audit`` scrubs the value before writing.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession, record_audit
from app.core.crypto import encrypt
from app.core.logging import get_logger
from app.db.models import RateApplication, User
from app.config import get_settings
from app.schemas.rate_application import (
    LoginCodeIn, LoginTestStatus, RateApplicationIn, RateApplicationOut,
)

router = APIRouter(prefix="/rate-application", tags=["rate-application"])
log = get_logger("api.rate_application")

def _out(row: RateApplication) -> RateApplicationOut:
    return RateApplicationOut(
        login_url=row.login_url,
        client_number=row.client_number,
        username=row.username,
        has_password=bool(row.encrypted_password),
        updated_at=row.updated_at,
        last_test_at=row.last_test_at,
        last_test_ok=row.last_test_ok,
        last_test_message=row.last_test_message,
        has_screenshot=bool(row.last_test_screenshot),
        session_state_saved_at=row.session_state_saved_at,
    )


async def _mine(session: AsyncSession, user: User) -> RateApplication | None:
    return await session.scalar(
        select(RateApplication).where(RateApplication.owner_user_id == user.id)
    )


@router.get("", response_model=RateApplicationOut)
async def read_rate_application(session: DbSession, user: CurrentUser):
    """What is set for the caller, or 404 if nothing is yet."""
    row = await _mine(session, user)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No rate application is set up yet.")
    return _out(row)


@router.put("", response_model=RateApplicationOut)
async def replace_rate_application(
    payload: RateApplicationIn, request: Request, session: DbSession, user: CurrentUser
):
    """Set, or change, the application and its login.

    A missing ``password`` keeps the stored one -- the form never shows it
    back, so it cannot resend it -- and is refused only when there is nothing
    stored to keep: a row without a password is a login that cannot happen,
    saved as if it could.
    """
    row = await _mine(session, user)
    before = None
    if row is None:
        if not payload.password:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A password is needed the first time the application is saved.",
            )
        row = RateApplication(owner_user_id=user.id)
        session.add(row)
    else:
        before = {
            "login_url": row.login_url,
            "client_number": row.client_number,
            "username": row.username,
        }

    changed_login = before is not None and (
        before["login_url"] != payload.login_url
        or before["client_number"] != payload.client_number
        or before["username"] != payload.username
    )
    row.login_url = payload.login_url
    row.client_number = payload.client_number
    row.username = payload.username
    if payload.password:
        row.encrypted_password = encrypt(payload.password)
    if changed_login:
        # The trust a site placed in "this device" was for that login. A
        # different one starts as a stranger, and the cookies would only
        # confuse the next test.
        row.encrypted_session_state = None
        row.session_state_saved_at = None

    await record_audit(
        session, user=user, action="update", entity="rate_application",
        entity_id=user.id, before=before,
        after={
            "login_url": payload.login_url,
            "client_number": payload.client_number,
            "username": payload.username,
            "password_changed": bool(payload.password),
        },
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    log.info(
        "rate_application_saved", user_id=user.id, password_changed=bool(payload.password)
    )
    return _out(row)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rate_application(request: Request, session: DbSession, user: CurrentUser):
    """Forget the application and its login. Idempotent: nothing set is a 204 too."""
    row = await _mine(session, user)
    if row is not None:
        await record_audit(
            session, user=user, action="delete", entity="rate_application",
            entity_id=user.id,
            before={"login_url": row.login_url, "username": row.username},
            request=request,
        )
        await session.delete(row)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _status_out(state: dict | None) -> LoginTestStatus:
    if state is None:
        return LoginTestStatus(status="idle")
    return LoginTestStatus(
        status=state["status"],
        message=state.get("message"),
        ok=state.get("ok"),
        has_screenshot=bool(state.get("has_screenshot")),
        tested_at=state.get("tested_at"),
    )


@router.post("/test-login", response_model=LoginTestStatus)
async def test_login(request: Request, session: DbSession, user: CurrentUser):
    """Start signing in to the saved application, changing nothing.

    Returns at once; the page polls ``/test-login/status``. A test cannot be
    one request because the application may ask for a one-time code, and
    the browser has to wait open on the worker while the owner reads their
    email -- see ``rate_app_test_state``.

    One attempt per click, and one at a time per owner. Login pages lock
    accounts after a handful of wrong tries, so this never retries, and a
    second click while a test is waiting for its code joins that test
    rather than opening a second browser and asking RMS for a second code.
    """
    from app.services import rate_app_test_state as state
    from app.workers.tasks_repair import test_rate_app_login

    row = await _mine(session, user)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Save the application details first, then test them."
        )

    current = state.read(user.id)
    if current and current["status"] in ("running", "needs_code"):
        return _status_out(current)

    await record_audit(
        session, user=user, action="test_login", entity="rate_application",
        entity_id=user.id, after={"login_url": row.login_url}, request=request,
    )
    await session.commit()

    state.clear(user.id)
    started = state.write(user.id, "running", message="Opening the login page…")
    test_rate_app_login.apply_async(args=[user.id], queue="browser")
    return _status_out(started)


@router.get("/test-login/status", response_model=LoginTestStatus)
async def test_login_status(session: DbSession, user: CurrentUser):
    """Where the test is: idle, running, needs_code, or done with its verdict."""
    from app.services import rate_app_test_state as state

    await _mine(session, user)  # 401 before anything, like every other route here
    return _status_out(state.read(user.id))


@router.post("/test-login/code", response_model=LoginTestStatus)
async def test_login_code(payload: LoginCodeIn, session: DbSession, user: CurrentUser):
    """The one-time code the application emailed, typed by the owner.

    Accepted only while a test is waiting for it. A code sent at any other
    time has nothing to go into and is refused rather than kept: a stored
    code is a code that could be typed into the wrong login later.
    """
    from app.services import rate_app_test_state as state

    await _mine(session, user)
    current = state.read(user.id)
    if not current or current["status"] != "needs_code":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No login is waiting for a code right now. Start a test first.",
        )
    state.offer_code(user.id, payload.code)
    return _status_out(state.write(user.id, "running", message="Entering the code…"))


@router.get("/screenshot")
async def test_screenshot(session: DbSession, user: CurrentUser):
    """The page the last test landed on. Taken after the submit; shows no password.

    Same traversal guard as the error artifacts: the stored path must resolve
    inside the artifact directory before anything is read.
    """
    row = await _mine(session, user)
    if row is None or not row.last_test_screenshot:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No screenshot from a login test yet.")
    root = Path(get_settings().artifact_dir).resolve()
    path = Path(row.last_test_screenshot).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That screenshot is no longer available."
        )
    return FileResponse(path, media_type="image/png", filename=path.name)
