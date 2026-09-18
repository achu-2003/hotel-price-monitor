"""The repricing rule, the room mapping, and the button that runs it.

Owner-scoped throughout: the settings row, the mappings and the run are
looked up by the caller, and a mapping can only be written for a room of a
hotel the caller owns and has marked as their own property.

THE RUN IS A JOB, NOT A REQUEST. It opens a browser on the worker and
walks RMS for a minute or two; the page polls ``/run/status``. One at a
time per owner: a second Apply while one is running joins it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, record_audit
from app.config import get_settings
from app.core.logging import get_logger
from app.db.models import (
    Hotel, RateApplication, RepricingAction, RepricingSettings, RmsRoomMapping, RoomType,
)
from app.schemas.repricing import (
    MappingIn, MappingOut, RepricingSettingsIn, RepricingSettingsOut, RunIn, RunStatus,
)

router = APIRouter(prefix="/repricing", tags=["repricing"])
log = get_logger("api.repricing")

KIND = "repricing"


async def _settings(session, user) -> RepricingSettings:
    row = await session.scalar(
        select(RepricingSettings).where(RepricingSettings.owner_user_id == user.id)
    )
    if row is None:
        row = RepricingSettings(owner_user_id=user.id)
        session.add(row)
        await session.flush()
    return row


def _settings_out(row: RepricingSettings) -> RepricingSettingsOut:
    return RepricingSettingsOut(
        auto_enabled=row.auto_enabled, position_pct=row.position_pct, max_step_pct=row.max_step_pct,
        floor_pct=row.floor_pct, ceiling_pct=row.ceiling_pct, min_competitors=row.min_competitors,
        round_to=row.round_to, channel=row.channel, weekend_pct=row.weekend_pct,
        sold_out_pct=row.sold_out_pct, updated_at=row.updated_at,
    )


@router.get("/settings", response_model=RepricingSettingsOut)
async def read_settings(session: DbSession, user: CurrentUser):
    row = await _settings(session, user)
    await session.commit()
    return _settings_out(row)


@router.put("/settings", response_model=RepricingSettingsOut)
async def replace_settings(payload: RepricingSettingsIn, request: Request, session: DbSession, user: CurrentUser):
    """The rule's numbers and the automatic switch. Audited: a rate that moves
    by itself has to be traceable to whoever turned the switch on."""
    row = await _settings(session, user)
    before = _settings_out(row).model_dump(mode="json", exclude={"updated_at"})
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    await record_audit(
        session, user=user, action="update", entity="repricing_settings", entity_id=user.id,
        before=before, after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _settings_out(row)


async def _own_room(session, user, room_type_id: int) -> RoomType:
    room = await session.scalar(
        select(RoomType).join(Hotel, RoomType.hotel_id == Hotel.id).where(
            RoomType.id == room_type_id, Hotel.owner_user_id == user.id, Hotel.is_own_property.is_(True),
        )
    )
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That room is not one of your own property's.")
    return room


@router.put("/mappings/{room_type_id}", response_model=MappingOut)
async def put_mapping(room_type_id: int, payload: MappingIn, request: Request, session: DbSession, user: CurrentUser):
    """Say which RMS row this room of yours is. Creates or replaces."""
    room = await _own_room(session, user, room_type_id)
    row = await session.scalar(select(RmsRoomMapping).where(RmsRoomMapping.room_type_id == room_type_id))
    before = None
    if row is None:
        row = RmsRoomMapping(owner_user_id=user.id, room_type_id=room_type_id, rms_room=payload.rms_room)
        session.add(row)
    else:
        before = MappingIn.model_validate(row).model_dump(mode="json")
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    await record_audit(
        session, user=user, action="update" if before else "create", entity="rms_room_mapping",
        entity_id=room_type_id, before=before, after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    return MappingOut(room_type_id=room_type_id, room_name=room.name, **payload.model_dump())


@router.delete("/mappings/{room_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping(room_type_id: int, request: Request, session: DbSession, user: CurrentUser):
    await _own_room(session, user, room_type_id)
    row = await session.scalar(select(RmsRoomMapping).where(RmsRoomMapping.room_type_id == room_type_id))
    if row is not None:
        await record_audit(
            session, user=user, action="delete", entity="rms_room_mapping", entity_id=room_type_id,
            before=MappingIn.model_validate(row).model_dump(mode="json"), request=request,
        )
        await session.delete(row)
        await session.commit()


def _run_out(state: dict | None) -> RunStatus:
    if not state:
        return RunStatus(status="idle")
    return RunStatus(
        status=state.get("status", "idle"), ok=state.get("ok"), message=state.get("message"),
        applied=state.get("applied"), failed=state.get("failed"), check_in=state.get("check_in"),
        has_screenshot=bool(state.get("has_screenshot")), updated_at=state.get("updated_at"),
    )


@router.post("/run", response_model=RunStatus)
async def start_run(payload: RunIn, request: Request, session: DbSession, user: CurrentUser):
    """Apply tonight's proposals to RMS (``manual``), or walk the grid and
    write nothing (``dry_run``). Returns at once; poll ``/run/status``."""
    from app.services import rate_app_test_state as state
    from app.workers.tasks_repricing import run_repricing

    app = await session.scalar(select(RateApplication).where(RateApplication.owner_user_id == user.id))
    if app is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Save the rate application login first (Rate app page).")
    mapped = await session.scalar(
        select(RmsRoomMapping.id).where(RmsRoomMapping.owner_user_id == user.id, RmsRoomMapping.is_enabled.is_(True))
    )
    if mapped is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Map at least one room to its RMS row first.")

    current = state.read(user.id, KIND)
    if current and current.get("status") == "running":
        return _run_out(current)

    await record_audit(
        session, user=user, action="run", entity="repricing", entity_id=user.id,
        after={"mode": payload.mode, "overrides": {str(k): str(v) for k, v in payload.overrides.items()}},
        request=request,
    )
    await session.commit()
    started = state.write(user.id, "running", KIND, message="Queued…", mode=payload.mode)
    # Celery's JSON turns int keys into strings anyway; send them that way.
    overrides = {str(k): str(v) for k, v in payload.overrides.items()}
    run_repricing.apply_async(args=[user.id, payload.mode, overrides], queue="browser")
    return _run_out(started)


@router.get("/run/status", response_model=RunStatus)
async def run_status(user: CurrentUser):
    from app.services import rate_app_test_state as state

    return _run_out(state.read(user.id, KIND))


@router.get("/actions/{action_id}/screenshot")
async def action_screenshot(action_id: int, session: DbSession, user: CurrentUser):
    """The grid after the run this action was part of."""
    row = await session.scalar(
        select(RepricingAction).where(RepricingAction.id == action_id, RepricingAction.owner_user_id == user.id)
    )
    if row is None or not row.screenshot_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No screenshot for that action.")
    path = Path(row.screenshot_path).resolve()
    root = Path(get_settings().artifact_dir).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That screenshot is no longer available.")
    return FileResponse(path, media_type="image/png")
