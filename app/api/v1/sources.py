"""Sources and their Terms of Service reviews.

The compliance gate lives here. ``sources.tos_reviewed_at`` is checked by the
dispatcher's query, so a source with no recorded review is never fetched — not
because anyone remembered to check, but because it cannot be selected.
Enabling a source without a review is refused by this router as well, which
makes the rule visible at the point where someone would try to break it.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.adapters import registry
from app.api.deps import AdminUser, CurrentUser, DbSession, get_object_or_404, record_audit
from app.core.logging import get_logger
from app.db.models import Source
from app.schemas.hotels import SourceCreate, SourceOut, SourceToSReview, SourceUpdate

router = APIRouter(prefix="/sources", tags=["sources"])
log = get_logger("api.sources")


def _to_out(source: Source) -> SourceOut:
    return SourceOut(
        **{c.name: getattr(source, c.name) for c in Source.__table__.columns
           if c.name in SourceOut.model_fields},
        is_usable=source.is_usable,
    )


@router.get("", response_model=list[SourceOut])
async def list_sources(session: DbSession, _user: CurrentUser):
    sources = (await session.scalars(select(Source).order_by(Source.code))).all()
    return [_to_out(s) for s in sources]


@router.get("/adapters", response_model=list[str])
async def list_adapters(_user: CurrentUser):
    """Adapter keys this build knows how to run.

    Exposed so the dashboard offers a list rather than a free-text field: a
    typo'd adapter key would otherwise only surface as a skipped target in the
    dispatcher's logs.
    """
    return registry.available_keys()


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: SourceCreate, request: Request, session: DbSession, admin: AdminUser
):
    if payload.adapter_key not in registry.available_keys():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown adapter_key {payload.adapter_key!r}. "
                   f"Known: {registry.available_keys()}",
        )
    if await session.scalar(select(Source).where(Source.code == payload.code)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A source with code {payload.code!r} already exists.",
        )

    # Created disabled, always. Enabling requires a recorded review, and
    # defaulting to enabled would make that gate depend on someone noticing it.
    source = Source(**payload.model_dump(), is_enabled=False)
    session.add(source)
    await session.flush()
    await record_audit(
        session, user=admin, action="create", entity="source", entity_id=source.id,
        after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()
    return _to_out(source)


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: int, payload: SourceUpdate, request: Request, session: DbSession, admin: AdminUser
):
    source = await get_object_or_404(session, Source, source_id, "Source")
    data = payload.model_dump(exclude_unset=True)

    if data.get("is_enabled") and source.tos_reviewed_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This source has no recorded Terms of Service review, so it "
                "cannot be enabled. POST /sources/{id}/tos-review first — the "
                "record is who approved it and when."
            ),
        )
    if "adapter_key" in data and data["adapter_key"] not in registry.available_keys():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown adapter_key {data['adapter_key']!r}.",
        )

    before = {c.name: getattr(source, c.name) for c in Source.__table__.columns}
    for field, value in data.items():
        setattr(source, field, value)

    await record_audit(
        session, user=admin, action="update", entity="source", entity_id=source_id,
        before=before, after=data, request=request,
    )
    await session.commit()
    log.info("source_updated", source_id=source_id, enabled=source.is_enabled)
    return _to_out(source)


@router.post("/{source_id}/tos-review", response_model=SourceOut)
async def record_tos_review(
    source_id: int,
    payload: SourceToSReview,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Record who reviewed this source's terms, and what they concluded.

    A negative review is recorded too, and leaves the source disabled. That is
    more useful than deleting it: it stops the same source being proposed
    again in six months by someone who was not there the first time.
    """
    source = await get_object_or_404(session, Source, source_id, "Source")

    source.tos_reviewed_at = payload.reviewed_at or date.today()
    source.tos_reviewed_by = payload.reviewed_by
    source.tos_notes = payload.notes
    if payload.approve:
        source.is_enabled = True
    else:
        # The review date stays set — it happened — but the source stays
        # disabled. Both halves of the dispatcher's gate matter: a reviewed
        # source that was declined must never be fetchable.
        source.is_enabled = False
        source.tos_notes = f"REVIEW DECLINED. {payload.notes or ''}".strip()

    await record_audit(
        session,
        user=admin,
        action="tos_review",
        entity="source",
        entity_id=source_id,
        after={
            "reviewed_by": payload.reviewed_by,
            "approved": payload.approve,
            "notes": payload.notes,
        },
        request=request,
    )
    await session.commit()

    log.info(
        "tos_review_recorded",
        source_id=source_id,
        approved=payload.approve,
        reviewed_by=payload.reviewed_by,
    )
    return _to_out(source)


@router.post("/{source_id}/robots-check", response_model=SourceOut)
async def check_robots(
    source_id: int, request: Request, session: DbSession, admin: AdminUser
):
    """Re-read this source's robots.txt and store the verdict.

    Run on demand rather than on a schedule because the answer only matters at
    fetch time, where it is checked again anyway. This endpoint exists so an
    operator can see the current answer without waiting for a check to fail.
    """
    source = await get_object_or_404(session, Source, source_id, "Source")
    if not source.base_domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This source has no base_domain, so there is no robots.txt to read.",
        )

    from app.adapters.playwright_base import build_user_agent
    from app.adapters.robots import RobotsChecker
    from app.config import get_settings

    settings = get_settings()
    url = source.base_domain
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    checker = RobotsChecker(
        build_user_agent(settings.browser_user_agent_suffix),
        cache=None,
        enabled=True,
    )
    verdict = checker.check(url)

    source.robots_checked_at = datetime.now(UTC)
    source.robots_allows = verdict.allowed
    if not verdict.allowed:
        # A refusal disables the source immediately. There is no workaround in
        # this codebase, and leaving it enabled would only queue failures.
        source.is_enabled = False
        log.warning("source_disabled_by_robots", source_id=source_id, url=url)

    await record_audit(
        session, user=admin, action="robots_check", entity="source", entity_id=source_id,
        after={"allowed": verdict.allowed, "reason": verdict.reason}, request=request,
    )
    await session.commit()
    return _to_out(source)
