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
    AutomaticIn, MappingIn, MappingOut, RepricingSettingsIn, RepricingSettingsOut,
    RunIn, RunStatus,
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
        sold_out_pct=row.sold_out_pct, benchmark_hotel_id=row.benchmark_hotel_id,
        benchmark_undercut=row.benchmark_undercut,
        benchmark_with_tax=row.benchmark_with_tax,
        benchmark_meal_plan=row.benchmark_meal_plan,
        benchmark_room_pairs={int(k): v for k, v in (row.benchmark_room_pairs or {}).items()},
        updated_at=row.updated_at,
    )


async def _own_competitor(session, user, hotel_id: int) -> None:
    """The benchmark has to be one of the caller's own active competitors.

    It feeds a prompt, so an id taken on trust would put another account's
    rates -- and their hotel's name -- into this owner's advice. The owner's
    OWN property is refused as well: a benchmark against yourself is a gap of
    zero, and a question with nothing in it.
    """
    ok = await session.scalar(
        select(Hotel.id).where(
            Hotel.id == hotel_id,
            Hotel.owner_user_id == user.id,
            Hotel.is_active.is_(True),
            Hotel.is_own_property.is_(False),
        )
    )
    if ok is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That hotel is not one of your active competitors."
        )


@router.get("/settings", response_model=RepricingSettingsOut)
async def read_settings(session: DbSession, user: CurrentUser):
    row = await _settings(session, user)
    await session.commit()
    return _settings_out(row)


@router.put("/settings", response_model=RepricingSettingsOut)
async def replace_settings(payload: RepricingSettingsIn, request: Request, session: DbSession, user: CurrentUser):
    """The rule's numbers. The automatic switch has its own route below.

    ``auto_enabled`` is accepted for older callers and ignored when absent:
    this form no longer carries it, and a missing field must not read as
    "off" and quietly stop the rates moving.
    """
    if payload.benchmark_hotel_id is not None:
        await _own_competitor(session, user, payload.benchmark_hotel_id)
    for room_type_id in payload.benchmark_room_pairs:
        # Same reason as the benchmark hotel: an id taken on trust would let
        # one account point its rule at another's room.
        await _own_room(session, user, room_type_id)
    row = await _settings(session, user)
    before = _settings_out(row).model_dump(mode="json", exclude={"updated_at"})
    for field, value in payload.model_dump().items():
        if field == "auto_enabled" and value is None:
            continue
        # JSONB keys are strings on the way in; the rule coerces on the way out.
        if field == "benchmark_room_pairs":
            value = {str(k): v for k, v in value.items()}
        setattr(row, field, value)
    await record_audit(
        session, user=user, action="update", entity="repricing_settings", entity_id=user.id,
        before=before, after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _settings_out(row)


@router.put("/automatic", response_model=RepricingSettingsOut)
async def set_automatic(payload: AutomaticIn, request: Request, session: DbSession, user: CurrentUser):
    """Turn the rule loose, or take it back.

    A route of its own, and not because one boolean deserves one. Everything
    else on the page proposes; this is the only control that lets a rate move
    with nobody watching, so it is sent alone, audited alone, and cannot ride
    along on a request that was about a percentage.

    Idempotent, and it says what it did rather than what it was asked to do:
    the toggle renders from the row that comes back, so a switch that failed
    to move cannot leave the page claiming it did.
    """
    row = await _settings(session, user)
    before = _settings_out(row).model_dump(mode="json", exclude={"updated_at"})
    row.auto_enabled = payload.enabled
    await record_audit(
        session, user=user, action="update", entity="repricing_settings", entity_id=user.id,
        before=before, after={"auto_enabled": payload.enabled}, request=request,
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

    # ONE ROOM: checked here, not in the worker. The worker would simply
    # find nothing to do and report "nothing to set", which reads like the
    # rule held the rate rather than like a room that is not the caller's or
    # is not mapped. Ownership is re-checked even though the id came from a
    # page we rendered, because the id arrives in a request body.
    if payload.only_room_type_id is not None:
        await _own_room(session, user, payload.only_room_type_id)
        one = await session.scalar(
            select(RmsRoomMapping.id).where(
                RmsRoomMapping.room_type_id == payload.only_room_type_id,
                RmsRoomMapping.owner_user_id == user.id,
                RmsRoomMapping.is_enabled.is_(True),
            )
        )
        if one is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That room is not mapped to an RMS row, so there is nothing to write for it.",
            )

    current = state.read(user.id, KIND)
    if current and current.get("status") == "running":
        return _run_out(current)

    await record_audit(
        session, user=user, action="run", entity="repricing", entity_id=user.id,
        after={
            "mode": payload.mode,
            "overrides": {str(k): str(v) for k, v in payload.overrides.items()},
            "only_room_type_id": payload.only_room_type_id,
            "channels": {str(k): {c: str(r) for c, r in v.items()} for k, v in payload.channels.items()},
            "skip_primary": payload.skip_primary,
        },
        request=request,
    )
    await session.commit()
    # ``scope`` is what stops the worker's redelivery guard from mistaking
    # "now do the next room" for a repeat of the room just done. See
    # tasks_repricing.run_repricing.
    scope = f"room:{payload.only_room_type_id}" if payload.only_room_type_id else "all"
    started = state.write(user.id, "running", KIND, message="Queued…", mode=payload.mode, scope=scope)
    # Celery's JSON turns int keys into strings anyway; send them that way.
    overrides = {str(k): str(v) for k, v in payload.overrides.items()}
    # Other channels only on a manual run: those are rates to WRITE, and a
    # Preview writes none.
    channels = ({str(k): {c: str(r) for c, r in v.items()} for k, v in payload.channels.items()}
                if payload.mode == "manual" else {})
    skip = payload.skip_primary if payload.mode == "manual" else []
    # ...and on a Preview, the channels to READ beyond the rule's own, which
    # is none unless asked. See RunIn.preview_channels.
    preview = payload.preview_channels if payload.mode == "dry_run" else []
    run_repricing.apply_async(
        args=[user.id, payload.mode, overrides, payload.only_room_type_id, channels, skip, preview],
        queue="browser",
    )
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
