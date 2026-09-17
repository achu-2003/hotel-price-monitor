"""Decide tonight's rates from the market and set them in RMS.

Two entry points. ``repricing.run`` does one owner's rooms once, in one of
three modes -- ``manual`` (the Apply button), ``auto`` (the scheduler), or
``dry_run`` (compute, log in, read the grid, write nothing) -- and
``repricing.auto_tick`` is the scheduler's call, which runs ``auto`` for
every owner whose switch is on.

ONE LOGIN, EVERY ROOM. The browser is opened once, walks to the grid once,
and reads and writes every mapped room on that visit. A login per room
would be five logins where one does.

THE GRID IS READ BEFORE IT IS WRITTEN, for two reasons that are the whole
of why this task is not a one-liner. The ratio between the RMS base rate
and the guest price on the site (``repricing.to_rms``) has to be taken on
the day, from the EP cell as it is now. And CP and MAP follow EP by the
supplement RMS holds between them today, which is only known by reading
them. So each room costs four reads and up to three writes.

EVERY DECISION IS A ROW in ``repricing_actions``, including the ones that
did nothing: a rate that moved by itself has to be able to say why, and a
rate that did NOT move when the page suggested it should has to be able to
say that too (held: below the floor; held: too few competitors; unchanged:
the grid already said so).

On the browser queue, like the login test, because it opens a browser.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import shared_task
from sqlalchemy import select

from app.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.core.logging import get_logger
from app.db.models import (
    PLANS, Hotel, PriceSeries, RateApplication, RepricingAction, RepricingSettings,
    RmsRoomMapping, RoomType,
)
from app.db.session import sync_session
from app.services import rate_app_test_state as state
from app.services import repricing as rule
from app.services.dates import local_today
from app.services.rate_app_login import _screenshot, attempt_login
from app.services.rate_app_rms import GridError, day_heading, open_grid, read_rate, write_rate

log = get_logger("tasks.repricing")

KIND = "repricing"
#: A run finishing inside this many minutes of the last one is a redelivery,
#: not a request. Shorter than the automatic cadence (30 minutes), longer
#: than a run takes (about four).
REPEAT_GUARD_MINUTES = 10


def priced_rows(session, owner_user_id: int, check_in, check_out, adults: int = 2):
    """The matrix query, synchronously: ``(series, hotel, room_name)`` for one night.

    The same filters as the dashboard's ``_priced_rows`` -- active hotels,
    active rooms, the owner's account -- so the repricer sees exactly the
    rows the comparison page shows and cannot decide on a room the page
    would not show.
    """
    return session.execute(
        select(PriceSeries, Hotel, RoomType.name)
        .join(Hotel, PriceSeries.hotel_id == Hotel.id)
        .join(RoomType, PriceSeries.room_type_id == RoomType.id)
        .where(
            PriceSeries.check_in == check_in,
            PriceSeries.check_out == check_out,
            PriceSeries.adults == adults,
            Hotel.is_active.is_(True),
            RoomType.is_active.is_(True),
            Hotel.owner_user_id == owner_user_id,
        )
        .order_by(Hotel.name, RoomType.sort_order)
    ).all()


def settings_for(session, owner_user_id: int) -> RepricingSettings:
    row = session.scalar(select(RepricingSettings).where(RepricingSettings.owner_user_id == owner_user_id))
    if row is None:
        row = RepricingSettings(owner_user_id=owner_user_id)
        session.add(row)
        session.flush()
    return row


def mappings_for(session, owner_user_id: int) -> list[RmsRoomMapping]:
    return list(session.scalars(
        select(RmsRoomMapping)
        .where(RmsRoomMapping.owner_user_id == owner_user_id, RmsRoomMapping.is_enabled.is_(True))
        .order_by(RmsRoomMapping.id)
    ))


def own_hotel_id(session, owner_user_id: int) -> int | None:
    return session.scalar(
        select(Hotel.id).where(
            Hotel.owner_user_id == owner_user_id, Hotel.is_own_property.is_(True), Hotel.is_active.is_(True)
        ).order_by(Hotel.name)
    )


def compute(session, owner_user_id: int, check_in, check_out) -> tuple[list[rule.Proposal], RepricingSettings, dict[int, RmsRoomMapping]]:
    """Tonight's proposals for the owner, with the settings and mapping they used."""
    settings = settings_for(session, owner_user_id)
    mappings = {m.room_type_id: m for m in mappings_for(session, owner_user_id)}
    own = own_hotel_id(session, owner_user_id)
    if own is None:
        return [], settings, mappings
    rows = priced_rows(session, owner_user_id, check_in, check_out)
    proposals = rule.propose(
        rows, own_hotel_id=own, rule=rule.Rule.from_row(settings),
        room_type_ids=set(mappings) or None,
    )
    return proposals, settings, mappings


def _record(session, owner_user_id: int, proposal: rule.Proposal, mapping: RmsRoomMapping | None,
            *, check_in, mode: str, status: str, plan: str | None = None, rate_type: str | None = None,
            current_rms=None, proposed_rms=None, applied_rms=None, reason: str | None = None,
            screenshot: str | None = None) -> RepricingAction:
    row = RepricingAction(
        owner_user_id=owner_user_id, room_type_id=proposal.room_type_id, room_name=proposal.room_name,
        rms_room=mapping.rms_room if mapping else None, rms_rate_type=rate_type, plan=plan,
        check_in=check_in, mode=mode, status=status,
        our_price=proposal.our_price, market_price=proposal.market, target_price=proposal.target,
        competitors=[c.as_json() for c in proposal.competitors],
        current_rms=current_rms, proposed_rms=proposed_rms, applied_rms=applied_rms,
        reason=reason, screenshot_path=screenshot,
    )
    session.add(row)
    return row


@shared_task(name="repricing.run", soft_time_limit=600, time_limit=660)
def run_repricing(owner_user_id: int, mode: str = "manual") -> dict[str, Any]:
    """Compute tonight's proposals and, unless ``mode`` is ``dry_run``, set them in RMS.

    ``mode``: ``manual`` (Apply pressed), ``auto`` (scheduler), ``dry_run``
    (everything but the write: the grid is read and the proposed RMS rates
    are recorded as ``proposed``).
    """
    dry = mode == "dry_run"
    tz = get_settings().timezone
    check_in = local_today(tz)
    check_out = check_in + timedelta(days=1)

    # A REDELIVERED TASK IS NOT A SECOND RUN. The broker acks late, so a
    # worker stopped between finishing and acknowledging hands the same
    # message to the next worker, which would sign in and walk the grid
    # again -- and in a writing mode, move rates that were just moved. A
    # run for this owner that finished within the last few minutes is the
    # run this message was for; say so and stop.
    last = state.read(owner_user_id, KIND)
    if last and last.get("status") == "done" and last.get("check_in") == check_in.isoformat():
        try:
            finished = datetime.fromisoformat(last["updated_at"])
        except (KeyError, ValueError):
            finished = None
        if finished and (datetime.now(UTC) - finished) < timedelta(minutes=REPEAT_GUARD_MINUTES):
            log.info("repricing_run_skipped_recent", owner_user_id=owner_user_id, mode=mode)
            return last

    with sync_session() as session:
        proposals, settings, mappings = compute(session, owner_user_id, check_in, check_out)
        channel = settings.channel
        round_to = settings.round_to
        app = session.scalar(select(RateApplication).where(RateApplication.owner_user_id == owner_user_id))
        if app is None:
            return state.write(owner_user_id, "done", KIND, ok=False,
                               message="No rate application is saved for this account.")
        login_url, client_number, username = app.login_url, app.client_number, app.username
        try:
            password = decrypt(app.encrypted_password)
        except Exception:  # noqa: BLE001
            return state.write(owner_user_id, "done", KIND, ok=False,
                               message="The saved password could not be read. Save it again on the Rate app page.")
        storage_state = None
        if app.encrypted_session_state:
            try:
                storage_state = json.loads(decrypt(app.encrypted_session_state))
            except Exception:  # noqa: BLE001
                storage_state = None

        # Nothing to write: say so without opening a browser, but leave the
        # held rows so the page can show why.
        actionable = [p for p in proposals if p.actionable and p.room_type_id in mappings]
        for p in proposals:
            if not p.actionable:
                _record(session, owner_user_id, p, mappings.get(p.room_type_id),
                        check_in=check_in, mode=mode, status="held", reason=p.held)
        session.commit()
        if not actionable:
            why = "no room has a proposal that clears the limits" if proposals else "no rooms are mapped, or no prices for tonight"
            return state.write(owner_user_id, "done", KIND, ok=True, applied=0,
                               message=f"Nothing to set: {why}.", check_in=check_in.isoformat())

    state.write(owner_user_id, "running", KIND, message="Signing in to the rate application…")
    outcome: dict[str, Any] = {"applied": 0, "failed": 0, "unchanged": 0, "lines": [], "shot": None}

    def on_grid(page, probe):
        state.write(owner_user_id, "running", KIND, message="Opening Room Rate Manager…")
        try:
            open_grid(page, channel=channel)
        except GridError as exc:
            outcome["error"] = f"could not open the {channel} rates: {exc}"
            return probe
        day = day_heading(page, 0)
        with sync_session() as session:
            for p in actionable:
                m = mappings[p.room_type_id]
                anchor = m.anchor_plan
                if anchor is None:
                    _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="held",
                            reason="no rate row is mapped for this room")
                    continue
                anchor_row = m.rate_type_for(anchor)
                state.write(owner_user_id, "running", KIND, message=f"Reading {m.rms_room}…")
                try:
                    current = {plan: read_rate(page, channel=channel, room=m.rms_room, rate_type=m.rate_type_for(plan), day_index=0)
                               for plan in PLANS if m.rate_type_for(plan)}
                except GridError as exc:
                    _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="failed",
                            reason=f"could not read the grid: {exc}")
                    outcome["failed"] += 1
                    continue
                if current.get(anchor) is None:
                    _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="held",
                            plan=anchor, rate_type=anchor_row,
                            reason=f"the {anchor} cell shows N/A, so there is no rate to scale from")
                    continue
                # THE RATIO IS TAKEN FROM A MATCHED PAIR. Right after an
                # apply, RMS shows the new rate and the site still shows the
                # old price (the channel takes a while), so grid-now over
                # site-now is not the channel's deal any more, and scaling
                # the same target by it would move the rate a second time.
                # While the site still shows the price the last move was
                # scaled from, that move's pre-move pair is the ratio.
                rms_for_ratio = current[anchor]
                prior = session.scalar(
                    select(RepricingAction).where(
                        RepricingAction.owner_user_id == owner_user_id,
                        RepricingAction.check_in == check_in,
                        RepricingAction.rms_room == m.rms_room,
                        RepricingAction.plan == anchor,
                        RepricingAction.status == "applied",
                    ).order_by(RepricingAction.created_at.desc())
                )
                if prior is not None and prior.our_price == p.our_price and prior.current_rms:
                    rms_for_ratio = prior.current_rms
                try:
                    new_anchor = rule.to_rms(p.target, p.our_price, rms_for_ratio, round_to=round_to)
                except ValueError as exc:
                    _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="held",
                            plan=anchor, rate_type=anchor_row, current_rms=current[anchor], reason=str(exc))
                    continue
                # The owner's rupee floor and ceiling are RMS numbers: checked
                # here, on the RMS number, and a breach holds the whole room.
                outside = rule.rms_bounds(new_anchor, floor=m.floor_amount, ceiling=m.ceiling_amount)
                if outside:
                    _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="held",
                            plan=anchor, rate_type=anchor_row, current_rms=current[anchor],
                            proposed_rms=new_anchor, reason=outside)
                    outcome["lines"].append(f"{m.rms_room} {anchor}: held, {outside}")
                    continue
                # The anchor plan is scaled from the site price; the others
                # keep their supplement over it (see repricing.follow).
                wanted = {anchor: new_anchor}
                for plan in PLANS:
                    if plan != anchor and current.get(plan) is not None:
                        wanted[plan] = rule.follow(new_anchor, current[anchor], current[plan], round_to=round_to)

                for plan, amount in wanted.items():
                    rate_type = m.rate_type_for(plan)
                    note = p.capped
                    if dry:
                        _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="proposed",
                                plan=plan, rate_type=rate_type, current_rms=current[plan], proposed_rms=amount, reason=note)
                        outcome["lines"].append(f"{m.rms_room} {plan}: {current[plan]:,.0f} → {amount:,.0f} (not written)")
                        continue
                    if int(current[plan]) == int(amount):
                        _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="unchanged",
                                plan=plan, rate_type=rate_type, current_rms=current[plan], proposed_rms=amount, reason=note)
                        outcome["unchanged"] += 1
                        continue
                    state.write(owner_user_id, "running", KIND, message=f"Setting {m.rms_room} {plan} to {amount:,.0f}…")
                    try:
                        now = write_rate(page, channel=channel, room=m.rms_room, rate_type=rate_type,
                                         day_index=0, amount=amount)
                    except GridError as exc:
                        _record(session, owner_user_id, p, m, check_in=check_in, mode=mode, status="failed",
                                plan=plan, rate_type=rate_type, current_rms=current[plan], proposed_rms=amount,
                                reason=str(exc))
                        outcome["failed"] += 1
                        outcome["lines"].append(f"{m.rms_room} {plan}: {exc}")
                        continue
                    ok = now is not None and int(now) == int(amount)
                    _record(session, owner_user_id, p, m, check_in=check_in, mode=mode,
                            status="applied" if ok else "failed", plan=plan, rate_type=rate_type,
                            current_rms=current[plan], proposed_rms=amount, applied_rms=now,
                            reason=note if ok else f"saved {amount:,.0f} but the grid shows {now}")
                    outcome["applied" if ok else "failed"] += 1
                    outcome["lines"].append(f"{m.rms_room} {plan}: {current[plan]:,.0f} → {amount:,.0f}" + ("" if ok else " (not confirmed)"))
            session.commit()
        outcome["shot"] = _screenshot(page, owner_user_id)
        outcome["day"] = day
        return probe

    probe = attempt_login(
        login_url=login_url, client_number=client_number, username=username, password=password,
        owner_user_id=owner_user_id, storage_state=storage_state, after_login=on_grid,
    )
    del password

    with sync_session() as session:
        # A run that logged in fresh may carry a new trusted-device cookie;
        # keep it the way the login test does.
        if probe.ok and probe.storage_state is not None:
            app = session.scalar(select(RateApplication).where(RateApplication.owner_user_id == owner_user_id))
            if app is not None:
                app.encrypted_session_state = encrypt(json.dumps(probe.storage_state))
                app.session_state_saved_at = datetime.now(UTC)
        if outcome.get("shot"):
            # The picture of the grid afterwards, on every row of this run.
            since = datetime.now(UTC) - timedelta(minutes=15)
            for row in session.scalars(select(RepricingAction).where(
                RepricingAction.owner_user_id == owner_user_id, RepricingAction.mode == mode,
                RepricingAction.check_in == check_in, RepricingAction.created_at >= since,
                RepricingAction.screenshot_path.is_(None),
            )):
                row.screenshot_path = outcome["shot"]
        session.commit()

    if not probe.ok:
        return state.write(owner_user_id, "done", KIND, ok=False, message=f"Could not sign in: {probe.message}")
    if outcome.get("error"):
        return state.write(owner_user_id, "done", KIND, ok=False, message=outcome["error"])
    verb = "Would set" if dry else "Set"
    summary = (
        f"{verb} {len(outcome['lines'])} rate(s) for {outcome.get('day', 'today')}"
        + (f"; {outcome['unchanged']} already right" if outcome["unchanged"] else "")
        + (f"; {outcome['failed']} failed" if outcome["failed"] else "")
        + ". " + "; ".join(outcome["lines"])
    )
    log.info("repricing_run_done", owner_user_id=owner_user_id, mode=mode, **{k: v for k, v in outcome.items() if k != "lines"})
    return state.write(owner_user_id, "done", KIND, ok=outcome["failed"] == 0, message=summary,
                       applied=outcome["applied"], failed=outcome["failed"], check_in=check_in.isoformat(),
                       has_screenshot=bool(outcome.get("shot")))


@shared_task(name="repricing.auto_tick")
def auto_tick() -> dict[str, Any]:
    """Run ``auto`` for every owner whose switch is on. The scheduler's call.

    Skips an owner whose last run is still going (the state says running),
    so a slow RMS cannot pile a second browser on top of the first.
    """
    started = []
    with sync_session() as session:
        owners = list(session.scalars(
            select(RepricingSettings.owner_user_id).where(RepricingSettings.auto_enabled.is_(True))
        ))
    for owner_user_id in owners:
        current = state.read(owner_user_id, KIND)
        if current and current.get("status") == "running":
            continue
        run_repricing.apply_async(args=[owner_user_id, "auto"], queue="browser")
        started.append(owner_user_id)
    return {"started": started, "enabled": len(owners)}
