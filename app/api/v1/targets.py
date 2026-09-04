"""Monitor targets, manual runs, check runs, and manual price entry.

THE CONTRACT THAT MATTERS
=========================
``POST /monitor-targets/{id}/run`` returns **202 with a check_run_id**, always.
A browser fetch takes 20-40 seconds; an HTTP request must never wait on one.
The dashboard polls ``/check-runs/{id}`` and shows progress. Making this
endpoint synchronous would tie up a web worker per click and time out behind
any proxy with a sane read timeout.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select

from app.adapters import registry
from app.api.deps import (
    AdminUser, CurrentUser, DbSession, get_object_or_404, owned_hotel_or_404, record_audit,
)
from app.core.logging import get_logger
from app.db.models import (
    AlertDefaults,
    CheckRun,
    DateStrategy,
    CheckRunStatus,
    CircuitState,
    Hotel,
    HotelSource,
    MonitorTarget,
    PriceSeries,
    Source,
)
from app.schemas.common import AcceptedRun, Page
from app.schemas.monitoring import (
    AlertDefaultsIn,
    AlertDefaultsOut,
    CheckRunOut,
    ManualEntryIn,
    MonitorTargetCreate,
    MonitorTargetOut,
    MonitorTargetUpdate,
)
from app.services import monitoring
from app.services.dates import local_today, resolve_stay_window
from app.services.ownership import scope_hotels

router = APIRouter(tags=["monitoring"])
log = get_logger("api.targets")


async def _assert_owns_target(session, target: MonitorTarget, user) -> None:
    """A target is reachable by its own id, so ownership comes from its hotel.

    Two hops: target -> hotel_source -> hotel. Skipping it would leave a
    schedule editable and deletable by anyone who can guess a small integer,
    on a hotel that is filtered out of every list they can see.
    """
    hotel_source = await get_object_or_404(
        session, HotelSource, target.hotel_source_id, "Hotel source"
    )
    await owned_hotel_or_404(session, hotel_source.hotel_id, user)


def _to_out(
    target: MonitorTarget, hotel: Hotel | None = None, today=None
) -> MonitorTargetOut:
    """Include the dates this target resolves to *today*.

    A rolling window is the field operators misread most often: "7 days out"
    looks static in the configuration and means a different night every day.
    Showing the resolved dates next to it removes the ambiguity.
    """
    out = MonitorTargetOut.model_validate(target)
    if hotel is not None:
        out.hotel_id = hotel.id
        out.hotel_name = hotel.name
    try:
        stay = resolve_stay_window(
            strategy=target.date_strategy,
            today=today or local_today(),
            fixed_check_in=target.fixed_check_in,
            fixed_check_out=target.fixed_check_out,
            lead_time_days=target.lead_time_days,
            length_of_stay_nights=target.length_of_stay_nights,
        )
    except ValueError:
        stay = None
    if stay is not None:
        out.resolved_check_in = stay.check_in
        out.resolved_check_out = stay.check_out
    return out


@router.get("/monitor-targets", response_model=Page[MonitorTargetOut])
async def list_targets(
    session: DbSession,
    user: CurrentUser,
    hotel_id: int | None = None,
    enabled: bool | None = None,
    circuit_state: CircuitState | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    statement = scope_hotels(
        select(MonitorTarget, Hotel)
        .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
        .join(Hotel, HotelSource.hotel_id == Hotel.id),
        user,
    )
    if hotel_id is not None:
        statement = statement.where(Hotel.id == hotel_id)
    if enabled is not None:
        statement = statement.where(MonitorTarget.is_enabled.is_(enabled))
    if circuit_state is not None:
        statement = statement.where(MonitorTarget.circuit_state == circuit_state)

    rows = (
        await session.execute(
            statement.order_by(Hotel.name, MonitorTarget.id).limit(limit).offset(offset)
        )
    ).all()
    today = local_today()
    return Page[MonitorTargetOut](
        items=[_to_out(t, h, today) for t, h in rows]
    )


@router.post(
    "/monitor-targets", response_model=MonitorTargetOut, status_code=status.HTTP_201_CREATED
)
async def create_target(
    payload: MonitorTargetCreate, request: Request, session: DbSession, admin: AdminUser
):
    hotel_source = await get_object_or_404(
        session, HotelSource, payload.hotel_source_id, "Hotel source"
    )
    await owned_hotel_or_404(session, hotel_source.hotel_id, admin)

    # A standing-rate source publishes one price with no notion of a night.
    # Watching it "7 days out" would store today's rate under next week's date:
    # a number that is plausible, wrong, and impossible to notice later.
    if (hotel_source.adapter_config or {}).get("standing_rate"):
        ahead = payload.lead_time_days
        if payload.date_strategy == DateStrategy.FIXED or (ahead or 0) > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This source publishes one current rate rather than pricing "
                    "per night, so it can only be watched for tonight. Set "
                    "'nights ahead' to 0 — the rate still updates whenever the "
                    "hotel changes it."
                ),
            )

    target = MonitorTarget(**payload.model_dump())
    # Due immediately: a newly added target should produce a first sighting on
    # the next sweep rather than in half an hour.
    target.next_run_at = datetime.now(UTC)
    session.add(target)
    await session.flush()

    await record_audit(
        session, user=admin, action="create", entity="monitor_target",
        entity_id=target.id, after=payload.model_dump(mode="json"), request=request,
    )
    await session.commit()

    hotel = await session.get(Hotel, hotel_source.hotel_id)
    return _to_out(target, hotel)


@router.patch("/monitor-targets/{target_id}", response_model=MonitorTargetOut)
async def update_target(
    target_id: int,
    payload: MonitorTargetUpdate,
    request: Request,
    session: DbSession,
    admin: AdminUser,
):
    """Interval and sensitivity changes take effect at the next sweep.

    Setting ``circuit_state`` to ``closed`` is how a paused target is resumed
    after whatever broke it has been fixed; the failure counter is cleared with
    it, otherwise the next single failure would re-open the circuit at once.
    """
    target = await get_object_or_404(session, MonitorTarget, target_id, "Monitor target")
    await _assert_owns_target(session, target, admin)
    data = payload.model_dump(exclude_unset=True)

    for field, value in data.items():
        setattr(target, field, value)

    if data.get("circuit_state") == CircuitState.CLOSED:
        target.consecutive_failures = 0
        target.circuit_opened_at = None
        target.next_run_at = datetime.now(UTC)

    await record_audit(
        session, user=admin, action="update", entity="monitor_target",
        entity_id=target_id, after=data, request=request,
    )
    await session.commit()
    return _to_out(target)


@router.delete("/monitor-targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: int, request: Request, session: DbSession, admin: AdminUser
):
    """Deleting a target stops the checks; the price history is untouched.

    ``price_series`` and ``price_observations`` are keyed on the offer, not on
    the target that happened to schedule the fetch, so removing a schedule can
    never destroy history.
    """
    target = await get_object_or_404(session, MonitorTarget, target_id, "Monitor target")
    await _assert_owns_target(session, target, admin)
    await session.delete(target)
    await record_audit(
        session, user=admin, action="delete", entity="monitor_target",
        entity_id=target_id, request=request,
    )
    await session.commit()


@router.post(
    "/monitor-targets/{target_id}/run",
    response_model=AcceptedRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_target_now(
    target_id: int, request: Request, session: DbSession, admin: AdminUser
):
    """Queue an immediate check. Returns at once with a run id to poll."""
    row = (
        await session.execute(
            select(MonitorTarget, HotelSource, Source, Hotel)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .join(Source, HotelSource.source_id == Source.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .where(MonitorTarget.id == target_id, Hotel.owner_user_id == admin.id)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Monitor target {target_id} does not exist.",
        )

    target, hotel_source, source, hotel = row

    if not source.is_usable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Source {source.code!r} is not usable: it must be enabled and "
                f"have a recorded Terms of Service review before anything is fetched."
            ),
        )

    # A manual run must respect the same gates the scheduler does. Without
    # these two checks, "Run now" quietly revives a target that was stopped for
    # a reason -- a robots.txt refusal, say -- and the operator sees the failure
    # in the Health tab minutes later instead of an answer at the button.
    if not hotel_source.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This hotel's {source.code!r} source is deactivated, so it is not "
                f"fetched. Deactivation is usually deliberate — a robots.txt "
                f"refusal, a bot wall, or a wrong booking engine. Reactivate the "
                f"source on the hotel page if the reason no longer applies."
            ),
        )
    if not target.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This target is disabled. Enable it first — running a disabled "
                "target by hand would hide whatever caused it to be turned off."
            ),
        )

    queue = registry.queue_for(source.adapter_key)
    if queue == "manual":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is a manual-entry source. Use POST /manual-entry instead.",
        )

    stay = resolve_stay_window(
        strategy=target.date_strategy,
        today=local_today(),
        fixed_check_in=target.fixed_check_in,
        fixed_check_out=target.fixed_check_out,
        lead_time_days=target.lead_time_days,
        length_of_stay_nights=target.length_of_stay_nights,
    )
    if stay is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This target's stay window is in the past; there is nothing left to check.",
        )

    check_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    session.add(
        CheckRun(
            id=check_run_id,
            monitor_target_id=target_id,
            triggered_by=f"user:{admin.id}",
            started_at=now,
            status=CheckRunStatus.RUNNING,
            check_in=stay.check_in,
            check_out=stay.check_out,
        )
    )
    await record_audit(
        session, user=admin, action="manual_run", entity="monitor_target",
        entity_id=target_id, after={"check_run_id": check_run_id}, request=request,
    )
    await session.commit()

    # Imported here rather than at module load: the API process should not
    # need the worker's dependency graph resolved just to serve a page.
    from app.workers.tasks_fetch import fetch_prices, group_to_payload
    from app.services.monitoring import DueGroup

    group = DueGroup(
        hotel_id=hotel.id,
        hotel_name=hotel.name,
        hotel_source_id=hotel_source.id,
        source_id=source.id,
        adapter_key=source.adapter_key,
        queue=queue,
        url=hotel_source.url,
        external_id=hotel_source.external_id,
        currency=hotel_source.currency,
        adapter_config=dict(hotel_source.adapter_config or {}),
        stay=stay,
        adults=target.adults,
        children=target.children,
        rooms=target.rooms,
        meal_plan_filter=target.meal_plan_filter,
        target_ids=[target.id],
        rate_limit_per_min=source.rate_limit_per_min,
    )
    fetch_prices.apply_async(
        args=[group_to_payload(group)],
        kwargs={"triggered_by": f"user:{admin.id}", "check_run_id": check_run_id},
        queue=queue,
    )

    log.info("manual_run_queued", target_id=target_id, check_run_id=check_run_id)
    return AcceptedRun(
        check_run_id=check_run_id, poll_url=f"/api/v1/check-runs/{check_run_id}"
    )


def _visible_check_runs(user):
    """Check runs for this account's targets, plus the hotel-less ones.

    A run with no ``monitor_target_id`` came from manual entry and carries no
    hotel of its own -- only an id, a status and a pair of dates. It stays
    visible because the dashboard polls it by id straight after creating it,
    and because there is nothing in it to keep.
    """
    return or_(
        CheckRun.monitor_target_id.is_(None),
        CheckRun.monitor_target_id.in_(
            select(MonitorTarget.id)
            .join(HotelSource, MonitorTarget.hotel_source_id == HotelSource.id)
            .join(Hotel, HotelSource.hotel_id == Hotel.id)
            .where(Hotel.owner_user_id == user.id)
        ),
    )


@router.get("/check-runs/{check_run_id}", response_model=CheckRunOut)
async def get_check_run(check_run_id: str, session: DbSession, user: CurrentUser):
    """What the dashboard polls after a manual run."""
    run = await session.scalar(
        select(CheckRun).where(CheckRun.id == check_run_id, _visible_check_runs(user))
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check run {check_run_id} does not exist.",
        )
    return CheckRunOut.model_validate(run)


@router.get("/check-runs", response_model=Page[CheckRunOut])
async def list_check_runs(
    session: DbSession,
    user: CurrentUser,
    monitor_target_id: int | None = None,
    status_filter: CheckRunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
):
    statement = (
        select(CheckRun)
        .where(_visible_check_runs(user))
        .order_by(CheckRun.started_at.desc())
        .limit(limit)
    )
    if monitor_target_id is not None:
        statement = statement.where(CheckRun.monitor_target_id == monitor_target_id)
    if status_filter is not None:
        statement = statement.where(CheckRun.status == status_filter)
    runs = (await session.scalars(statement)).all()
    return Page[CheckRunOut](items=[CheckRunOut.model_validate(r) for r in runs])


@router.post("/manual-entry", response_model=AcceptedRun, status_code=status.HTTP_202_ACCEPTED)
async def manual_entry(
    payload: ManualEntryIn, request: Request, session: DbSession, admin: AdminUser
):
    """Record hand-entered prices for a hotel no adapter can cover.

    Handed to the same worker pipeline as a scraped fetch rather than written
    here, so a manual price goes through the identical offer_key, comparison,
    debounce and notification path. A manually tracked hotel then behaves
    exactly like an automated one — which is the point of having the fallback
    at all.
    """
    hotel_source = await get_object_or_404(
        session, HotelSource, payload.hotel_source_id, "Hotel source"
    )
    await owned_hotel_or_404(session, hotel_source.hotel_id, admin)
    check_run_id = str(uuid.uuid4())

    session.add(
        CheckRun(
            id=check_run_id,
            monitor_target_id=None,
            triggered_by=f"manual:{admin.id}",
            started_at=datetime.now(UTC),
            status=CheckRunStatus.RUNNING,
            check_in=payload.offers[0].check_in,
            check_out=payload.offers[0].check_out,
        )
    )
    await record_audit(
        session, user=admin, action="manual_entry", entity="hotel_source",
        entity_id=hotel_source.id,
        after={"offers": len(payload.offers), "check_run_id": check_run_id},
        request=request,
    )
    await session.commit()

    from app.workers.tasks_fetch import record_manual_offers

    record_manual_offers.apply_async(
        args=[payload.model_dump(mode="json"), check_run_id, f"manual:{admin.id}"],
        queue="http",
    )
    return AcceptedRun(
        check_run_id=check_run_id, poll_url=f"/api/v1/check-runs/{check_run_id}"
    )


# -- alert sensitivity -----------------------------------------------
async def _alert_defaults_out(session, row: AlertDefaults) -> AlertDefaultsOut:
    """The stored values, with the cheapest and dearest room in the portfolio.

    Those two numbers are the point of the panel. Both floors have to be
    cleared, so a rupee amount that is loud on the cheapest room can be
    completely silent on the dearest, and the only way to know is to see them.
    """
    extremes = (
        await session.execute(
            select(func.min(PriceSeries.last_price), func.max(PriceSeries.last_price))
            .where(PriceSeries.last_price.is_not(None))
        )
    ).one()
    return AlertDefaultsOut(
        min_delta_abs=row.min_delta_abs,
        min_delta_pct=row.min_delta_pct,
        confirm_checks=row.confirm_checks,
        cheapest_room=extremes[0],
        dearest_room=extremes[1],
    )


@router.get("/alert-defaults", response_model=AlertDefaultsOut)
async def read_alert_defaults(session: DbSession, _user: CurrentUser):
    """How big a move has to be, for any hotel that has not overridden it."""
    row = await session.get(AlertDefaults, 1)
    if row is None:
        # The window between deploying this code and running its migration.
        # Answer with what the comparison engine is actually using rather than
        # a 404, which would read as "this deployment has no sensitivity".
        current = monitoring.default_thresholds()
        row = AlertDefaults(
            id=1,
            min_delta_abs=current.min_delta_abs,
            min_delta_pct=current.min_delta_pct,
            confirm_checks=current.confirm_checks,
        )
    return await _alert_defaults_out(session, row)


@router.put("/alert-defaults", response_model=AlertDefaultsOut)
async def replace_alert_defaults(
    payload: AlertDefaultsIn, request: Request, session: DbSession, admin: AdminUser
):
    """Change the deployment-wide sensitivity.

    Takes effect within a minute -- see ``_DEFAULTS_TTL_SECONDS`` -- without a
    restart, which is the entire reason this is a table rather than an
    environment variable. Hotels carrying their own override are unaffected;
    this is only what the rest fall back to.
    """
    row = await session.get(AlertDefaults, 1)
    before = None
    if row is None:
        row = AlertDefaults(id=1)
        session.add(row)
    else:
        before = {
            "min_delta_abs": str(row.min_delta_abs),
            "min_delta_pct": str(row.min_delta_pct),
            "confirm_checks": row.confirm_checks,
        }

    row.min_delta_abs = payload.min_delta_abs
    row.min_delta_pct = payload.min_delta_pct
    row.confirm_checks = payload.confirm_checks

    await record_audit(
        session, user=admin, action="update", entity="alert_defaults",
        entity_id="1", before=before,
        after={
            "min_delta_abs": str(payload.min_delta_abs),
            "min_delta_pct": str(payload.min_delta_pct),
            "confirm_checks": payload.confirm_checks,
        },
        request=request,
    )
    await session.commit()
    await session.refresh(row)

    # The workers cache this for a minute each. Clearing it here only helps the
    # process that served the request -- the others age out on their own -- but
    # it makes the dashboard's own reads agree with the form immediately.
    monitoring.forget_stored_defaults()

    log.info("alert_defaults_updated",
             min_delta_abs=str(row.min_delta_abs),
             min_delta_pct=str(row.min_delta_pct),
             confirm_checks=row.confirm_checks)
    return await _alert_defaults_out(session, row)

