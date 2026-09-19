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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from celery import shared_task
from sqlalchemy import or_, select

from app.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.core.logging import get_logger
from app.db.models import (
    PLANS, Hotel, PriceSeries, RateApplication, RepricingAction, RepricingAdvice,
    RepricingSettings, RmsRoomMapping, RoomType,
)
from app.db.session import sync_session
from app.services import rate_app_test_state as state
from app.services import repricing as rule
from app.services import repricing_data as data
from app.services import repricing_advisor as advisor
from app.services.repricing_advisor_openai import completer_from_settings
from app.services.dates import local_today
from app.services.rate_app_login import _screenshot, attempt_login
from app.services.rate_app_rms import (
    GridError, channel_names, day_heading, expand_channel, open_grid, read_rate, write_rate,
)

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
    own_rule = rule.Rule.from_row(settings)
    history = data.one_night(session.execute(data.history_stmt(owner_user_id, check_in)).all())
    proposals = rule.propose(
        rows, own_hotel_id=own, rule=own_rule,
        room_type_ids=set(mappings) or None,
        usual=rule.usual_gaps(history, own_hotel_id=own, min_competitors=own_rule.min_competitors),
        night=check_in,
        moved_today=set(session.scalars(data.moved_stmt(owner_user_id, check_in, settings.channel))),
    )
    return proposals, settings, mappings


def _record(session, owner_user_id: int, proposal: rule.Proposal, mapping: RmsRoomMapping | None,
            *, check_in, mode: str, status: str, channel: str | None = None,
            plan: str | None = None, rate_type: str | None = None,
            current_rms=None, proposed_rms=None, applied_rms=None, reason: str | None = None,
            screenshot: str | None = None) -> RepricingAction:
    row = RepricingAction(
        owner_user_id=owner_user_id, room_type_id=proposal.room_type_id, room_name=proposal.room_name,
        channel=channel, rms_room=mapping.rms_room if mapping else None, rms_rate_type=rate_type, plan=plan,
        check_in=check_in, mode=mode, status=status,
        our_price=proposal.our_price, market_price=proposal.market, target_price=proposal.target,
        competitors=[c.as_json() for c in proposal.competitors],
        current_rms=current_rms, proposed_rms=proposed_rms, applied_rms=applied_rms,
        reason=reason, screenshot_path=screenshot,
    )
    session.add(row)
    return row


def _recent_prices(session, room_type_id: int, limit: int = 8) -> tuple:
    """Our own price for this room over the last few nights, oldest first.

    Context the median cannot supply: a room the owner has already cut twice
    this week is a different decision from one that has not moved. Cheap --
    one indexed read per room, and an empty result is fine.
    """
    rows = session.execute(
        select(PriceSeries.check_in, PriceSeries.current_price)
        .where(
            PriceSeries.room_type_id == room_type_id,
            PriceSeries.current_price.isnot(None),
        )
        .order_by(PriceSeries.check_in.desc())
        .limit(limit)
    ).all()
    return tuple(price for _, price in reversed(rows))


def shadow_advice(session, owner_user_id: int, proposals, settings, *, check_in, mode: str) -> int:
    """Record what a model would have proposed, beside what the rule did.

    SHADOW ONLY. This reads ``proposals`` and writes ``repricing_advice``
    rows; it returns nothing to the caller and mutates nothing it was given,
    so no rate can move because of anything here. That is enforced by the
    signature as much as by the body -- there is no value to misuse.

    Every failure is swallowed. The advisor is an experiment running beside
    a system that sets real rates, and an experiment must not be able to stop
    a rate run. A night with no advice is a blank in the comparison, which is
    the correct record of what happened.
    """
    app_settings = get_settings()
    if not app_settings.advisor_enabled:
        return 0
    complete = completer_from_settings(app_settings)
    if complete is None:
        log.info("advisor_skipped_no_key", owner_user_id=owner_user_id)
        return 0

    own_rule = rule.Rule.from_row(settings)
    hotel_name = session.scalar(
        select(Hotel.name).join(RoomType, RoomType.hotel_id == Hotel.id)
        .where(RoomType.id == proposals[0].room_type_id)
    ) if proposals else None

    written = 0
    for p in proposals:
        # Only rooms the rule itself could price. A room it held on (no tier,
        # no price of ours, too few competitors) has nothing for a model to
        # improve on either, and asking anyway would spend a call to learn
        # what the rule already said.
        if p.our_price is None or p.market is None or not p.competitors:
            continue

        ask = advisor.Ask(
            hotel=hotel_name or "the owner's hotel",
            room_name=p.room_name,
            tier_label=p.tier_label,
            check_in=check_in,
            our_price=p.our_price,
            competitors=tuple((c.hotel, c.room, c.price) for c in p.competitors),
            rule_position_pct=own_rule.position_pct,
            usual_gap=p.usual_gap,
            our_recent=_recent_prices(session, p.room_type_id),
            notes=tuple(
                f"{c.hotel}'s price is approximate ({c.note})"
                for c in p.competitors if c.note
            ) + ((f"Sold out tonight in this tier: {', '.join(p.sold_out)}",) if p.sold_out else ()),
        )
        try:
            advice = advisor.advise(
                ask,
                complete=complete,
                model=app_settings.openai_model,
                max_position_pct=app_settings.advisor_max_position_pct,
            )
        except Exception as exc:  # noqa: BLE001 -- never fatal, see docstring
            log.warning("advisor_unexpected", room=p.room_name, error=str(exc)[:200])
            advice = advisor.Advice(error=f"unexpected: {type(exc).__name__}")

        # The advisor's position through the SAME arithmetic the rule's went
        # through -- aim, then every limit. This is the number the comparison
        # rests on, and computing it any other way would compare a bounded
        # proposal against an unbounded one.
        advisor_target = None
        if advice.usable:
            shadow_rule = replace(own_rule, position_pct=advice.position_pct)
            wanted = rule.aim(p.market, shadow_rule, gap=p.usual_gap or Decimal("1"),
                              demand_pct=p.demand_pct)
            advisor_target, _held, _capped = rule.guard(p.our_price, wanted, shadow_rule)

        session.add(RepricingAdvice(
            owner_user_id=owner_user_id,
            room_type_id=p.room_type_id,
            room_name=p.room_name,
            check_in=check_in,
            mode=mode,
            our_price=p.our_price,
            market_price=p.market,
            competitors=[c.as_json() for c in p.competitors],
            rule_position_pct=own_rule.position_pct,
            rule_target=p.target,
            advisor_position_pct=advice.position_pct,
            advisor_target=advisor_target,
            advisor_confidence=advice.confidence,
            rationale=advice.rationale,
            key_factors=list(advice.key_factors),
            clamped=advice.clamped,
            error=advice.error,
            model=advice.model,
            latency_ms=advice.latency_ms,
        ))
        written += 1

    log.info("advisor_shadow_recorded", owner_user_id=owner_user_id, rows=written)
    return written


@shared_task(name="repricing.run", soft_time_limit=600, time_limit=660)
def run_repricing(owner_user_id: int, mode: str = "manual",
                  overrides: dict[str, str] | None = None,
                  only_room_type_id: int | None = None,
                  channels: dict[str, dict[str, str]] | None = None,
                  skip_primary: list[int] | None = None) -> dict[str, Any]:
    """Compute tonight's proposals and, unless ``mode`` is ``dry_run``, set them in RMS.

    ``mode``: ``manual`` (Apply pressed), ``auto`` (scheduler), ``dry_run``
    (everything but the write: the grid is read and the proposed RMS rates
    are recorded as ``proposed``). ``overrides`` are guest prices the owner
    typed on the page, by room type id; the scheduler never sends any.

    ``only_room_type_id`` narrows the run to ONE of the owner's rooms and
    leaves every other rate exactly as it is. The scheduler never sends it --
    automatic mode is the whole property or nothing -- and the caller has
    already checked the room is the owner's and is mapped.

    OTHER CHANNELS ARE THE OWNER'S CALL. ``channels`` is ``{room_type_id:
    {channel: rms_rate}}`` -- the other RMS channels (Goibibo, Expedia, the
    Book Now button...) the owner ticked for a room, each with the RMS rate
    for the room's anchor plan that the page showed and they accepted. Only
    those are written, with exactly that number; the channel's other plans
    keep their supplement over it; the room's RMS floor and ceiling still
    hold. ``skip_primary`` lists rooms whose main channel was unticked. The
    scheduler sends neither: automatic mode never touches another channel.
    A Preview reads every channel the grid lists, writing nothing, so the
    page can show what each holds today.
    """
    typed = {int(k): Decimal(v) for k, v in (overrides or {}).items()} if mode != "auto" else {}
    picks = ({int(k): {ch: Decimal(v) for ch, v in chans.items()} for k, chans in (channels or {}).items()}
             if mode == "manual" else {})
    skip = {int(k) for k in (skip_primary or [])} if mode == "manual" else set()
    if mode == "auto":
        only_room_type_id = None
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
    # ``scope`` is part of what makes a message a REPEAT. Without it, an
    # owner applying the Deluxe and then the Suite a minute later had the
    # second click silently swallowed as a redelivery of the first -- the
    # page said "done", and the Suite's rate had never been touched. A run
    # for a different room is a different run.
    scope = f"room:{only_room_type_id}" if only_room_type_id else "all"
    if picks or skip:
        chosen = sorted(f"{rid}:{ch}" for rid, chans in picks.items() for ch in chans)
        scope += "|" + ",".join(chosen) + "|skip:" + ",".join(map(str, sorted(skip)))
    last = state.read(owner_user_id, KIND)
    if (last and last.get("status") == "done"
            and last.get("check_in") == check_in.isoformat()
            and last.get("scope", "all") == scope):
        try:
            finished = datetime.fromisoformat(last["updated_at"])
        except (KeyError, ValueError):
            finished = None
        if finished and (datetime.now(UTC) - finished) < timedelta(minutes=REPEAT_GUARD_MINUTES):
            log.info("repricing_run_skipped_recent", owner_user_id=owner_user_id, mode=mode)
            return last

    with sync_session() as session:
        proposals, settings, mappings = compute(session, owner_user_id, check_in, check_out)

        # ONE ROOM, and narrowed BEFORE the advisor below, so applying a
        # single room costs a single call rather than one per room of a
        # property whose other rates this run will not touch. The shadow log
        # therefore records the rooms a run considered, not every room that
        # had a market that night -- the nightly automatic run is what fills
        # it in full.
        if only_room_type_id is not None:
            proposals = [p for p in proposals if p.room_type_id == only_room_type_id]
            mappings = {k: v for k, v in mappings.items() if k == only_room_type_id}
            typed = {k: v for k, v in typed.items() if k == only_room_type_id}
            picks = {k: v for k, v in picks.items() if k == only_room_type_id}
            skip = {k for k in skip if k == only_room_type_id}

        # Shadow only, and deliberately before the login: it must not be able
        # to delay or fail a run that is about to touch real rates, and a
        # failure here has to show up before the browser costs four minutes.
        try:
            shadow_advice(session, owner_user_id, proposals, settings,
                          check_in=check_in, mode=mode)
            session.commit()
        except Exception as exc:  # noqa: BLE001 -- the experiment never stops the rate run
            session.rollback()
            log.warning("advisor_shadow_failed", owner_user_id=owner_user_id, error=str(exc)[:200])

        # The owner's typed prices replace the rule's AFTER the shadow advice,
        # which compares a model with the rule and not with the owner.
        proposals = [rule.override(p, typed[p.room_type_id]) if p.room_type_id in typed else p
                     for p in proposals]

        channel = settings.channel
        round_to = settings.round_to
        app = session.scalar(select(RateApplication).where(RateApplication.owner_user_id == owner_user_id))
        if app is None:
            return state.write(owner_user_id, "done", KIND, scope=scope, ok=False,
                               message="No rate application is saved for this account.")
        login_url, client_number, username = app.login_url, app.client_number, app.username
        try:
            password = decrypt(app.encrypted_password)
        except Exception:  # noqa: BLE001
            return state.write(owner_user_id, "done", KIND, scope=scope, ok=False,
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
        by_room = {p.room_type_id: p for p in proposals}
        picks = {rid: chans for rid, chans in picks.items() if rid in mappings and rid in by_room}
        for p in proposals:
            if not p.actionable:
                _record(session, owner_user_id, p, mappings.get(p.room_type_id), channel=channel,
                        check_in=check_in, mode=mode, status="held", reason=p.held)
        session.commit()
        if not actionable and not picks and not dry:
            why = "no room has a proposal that clears the limits" if proposals else "no rooms are mapped, or no prices for tonight"
            return state.write(owner_user_id, "done", KIND, scope=scope, ok=True, applied=0,
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
        listed = channel_names(page)
        with sync_session() as session:
            row = settings_for(session, owner_user_id)
            if listed and row.known_channels != listed:
                row.known_channels = listed
            session.commit()
        with sync_session() as session:
            for p in actionable:
                m = mappings[p.room_type_id]
                anchor = m.anchor_plan
                if anchor is None:
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="held",
                            reason="no rate row is mapped for this room")
                    continue
                anchor_row = m.rate_type_for(anchor)
                state.write(owner_user_id, "running", KIND, message=f"Reading {m.rms_room}…")
                try:
                    current = {plan: read_rate(page, channel=channel, room=m.rms_room, rate_type=m.rate_type_for(plan), day_index=0)
                               for plan in PLANS if m.rate_type_for(plan)}
                except GridError as exc:
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="failed",
                            reason=f"could not read the grid: {exc}")
                    outcome["failed"] += 1
                    continue
                if current.get(anchor) is None:
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="held",
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
                        or_(RepricingAction.channel == channel, RepricingAction.channel.is_(None)),
                    ).order_by(RepricingAction.created_at.desc())
                )
                if prior is not None and prior.our_price == p.our_price and prior.current_rms:
                    rms_for_ratio = prior.current_rms
                try:
                    new_anchor = rule.to_rms(p.target, p.our_price, rms_for_ratio, round_to=round_to)
                except ValueError as exc:
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="held",
                            plan=anchor, rate_type=anchor_row, current_rms=current[anchor], reason=str(exc))
                    continue
                # The owner's rupee floor and ceiling are RMS numbers: checked
                # here, on the RMS number, and a breach holds the whole room.
                outside = rule.rms_bounds(new_anchor, floor=m.floor_amount, ceiling=m.ceiling_amount)
                if outside:
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="held",
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

                if p.room_type_id in skip:
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode,
                            status="held", plan=anchor, rate_type=anchor_row, current_rms=current[anchor],
                            proposed_rms=new_anchor, reason=f"{channel} was left unticked")
                    continue
                for plan, amount in wanted.items():
                    rate_type = m.rate_type_for(plan)
                    note = p.capped
                    if dry:
                        _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="proposed",
                                plan=plan, rate_type=rate_type, current_rms=current[plan], proposed_rms=amount, reason=note)
                        outcome["lines"].append(f"{m.rms_room} {plan}: {current[plan]:,.0f} → {amount:,.0f} (not written)")
                        continue
                    if int(current[plan]) == int(amount):
                        _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="unchanged",
                                plan=plan, rate_type=rate_type, current_rms=current[plan], proposed_rms=amount, reason=note)
                        outcome["unchanged"] += 1
                        continue
                    state.write(owner_user_id, "running", KIND, message=f"Setting {m.rms_room} {plan} to {amount:,.0f}…")
                    try:
                        now = write_rate(page, channel=channel, room=m.rms_room, rate_type=rate_type,
                                         day_index=0, amount=amount)
                    except GridError as exc:
                        _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode, status="failed",
                                plan=plan, rate_type=rate_type, current_rms=current[plan], proposed_rms=amount,
                                reason=str(exc))
                        outcome["failed"] += 1
                        outcome["lines"].append(f"{m.rms_room} {plan}: {exc}")
                        continue
                    ok = now is not None and int(now) == int(amount)
                    _record(session, owner_user_id, p, m, channel=channel, check_in=check_in, mode=mode,
                            status="applied" if ok else "failed", plan=plan, rate_type=rate_type,
                            current_rms=current[plan], proposed_rms=amount, applied_rms=now,
                            reason=note if ok else f"saved {amount:,.0f} but the grid shows {now}")
                    outcome["applied" if ok else "failed"] += 1
                    outcome["lines"].append(f"{m.rms_room} {plan}: {current[plan]:,.0f} → {amount:,.0f}" + ("" if ok else " (not confirmed)"))
            session.commit()

        # ── the other channels ──
        # A Preview reads every one the grid lists, for every mapped room, so
        # the page can show what each holds today. A manual run writes only
        # what the owner ticked, with the number they accepted.
        others = [c for c in listed if c != channel]
        if dry:
            work = {ch: list(mappings) for ch in others}
        else:
            work = {}
            for rid, chans in picks.items():
                for ch in chans:
                    if ch != channel:  # the main channel is the loop above, never twice
                        work.setdefault(ch, []).append(rid)
        with sync_session() as session:
            for ch, rooms in work.items():
                try:
                    expand_channel(page, channel=ch)
                except GridError as exc:
                    for rid in rooms:
                        _record(session, owner_user_id, by_room[rid], mappings[rid], channel=ch, check_in=check_in,
                                mode=mode, status="failed", reason=f"could not open {ch}: {exc}")
                    outcome["failed"] += 0 if dry else len(rooms)
                    continue
                for rid in rooms:
                    p, m = by_room.get(rid), mappings[rid]
                    if p is None or m.anchor_plan is None:
                        continue
                    anchor = m.anchor_plan
                    state.write(owner_user_id, "running", KIND, message=f"Reading {ch} / {m.rms_room}…")
                    try:
                        current = {plan: read_rate(page, channel=ch, room=m.rms_room, rate_type=m.rate_type_for(plan), day_index=0)
                                   for plan in PLANS if m.rate_type_for(plan)}
                    except GridError as exc:
                        _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode,
                                status="failed", reason=f"could not read {ch}: {exc}")
                        if not dry:
                            outcome["failed"] += 1
                            outcome["lines"].append(f"{ch} {m.rms_room}: {exc}")
                        continue
                    if dry:
                        for plan, value in current.items():
                            _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode,
                                    status="read", plan=plan, rate_type=m.rate_type_for(plan), current_rms=value)
                        continue
                    new_anchor = picks[rid][ch]
                    if current.get(anchor) is None:
                        _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode, status="held",
                                plan=anchor, rate_type=m.rate_type_for(anchor), proposed_rms=new_anchor,
                                reason=f"the {ch} {anchor} cell shows N/A")
                        outcome["lines"].append(f"{ch} {m.rms_room}: held, the cell shows N/A")
                        continue
                    outside = rule.rms_bounds(new_anchor, floor=m.floor_amount, ceiling=m.ceiling_amount)
                    if outside:
                        _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode, status="held",
                                plan=anchor, rate_type=m.rate_type_for(anchor), current_rms=current[anchor],
                                proposed_rms=new_anchor, reason=outside)
                        outcome["lines"].append(f"{ch} {m.rms_room}: held, {outside}")
                        continue
                    wanted = {anchor: new_anchor}
                    for plan in PLANS:
                        if plan != anchor and current.get(plan) is not None:
                            wanted[plan] = rule.follow(new_anchor, current[anchor], current[plan], round_to=round_to)
                    for plan, amount in wanted.items():
                        rate_type = m.rate_type_for(plan)
                        if int(current[plan]) == int(amount):
                            _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode,
                                    status="unchanged", plan=plan, rate_type=rate_type,
                                    current_rms=current[plan], proposed_rms=amount, reason="ticked by you")
                            outcome["unchanged"] += 1
                            continue
                        state.write(owner_user_id, "running", KIND, message=f"Setting {ch} {m.rms_room} {plan} to {amount:,.0f}…")
                        try:
                            now = write_rate(page, channel=ch, room=m.rms_room, rate_type=rate_type,
                                             day_index=0, amount=amount)
                        except GridError as exc:
                            _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode,
                                    status="failed", plan=plan, rate_type=rate_type, current_rms=current[plan],
                                    proposed_rms=amount, reason=str(exc))
                            outcome["failed"] += 1
                            outcome["lines"].append(f"{ch} {m.rms_room} {plan}: {exc}")
                            continue
                        ok = now is not None and int(now) == int(amount)
                        _record(session, owner_user_id, p, m, channel=ch, check_in=check_in, mode=mode,
                                status="applied" if ok else "failed", plan=plan, rate_type=rate_type,
                                current_rms=current[plan], proposed_rms=amount, applied_rms=now,
                                reason="ticked by you" if ok else f"saved {amount:,.0f} but the grid shows {now}")
                        outcome["applied" if ok else "failed"] += 1
                        outcome["lines"].append(f"{ch} {m.rms_room} {plan}: {current[plan]:,.0f} → {amount:,.0f}"
                                                + ("" if ok else " (not confirmed)"))
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
        return state.write(owner_user_id, "done", KIND, scope=scope, ok=False, message=f"Could not sign in: {probe.message}")
    if outcome.get("error"):
        return state.write(owner_user_id, "done", KIND, scope=scope, ok=False, message=outcome["error"])
    verb = "Would set" if dry else "Set"
    summary = (
        f"{verb} {len(outcome['lines'])} rate(s) for {outcome.get('day', 'today')}"
        + (f"; {outcome['unchanged']} already right" if outcome["unchanged"] else "")
        + (f"; {outcome['failed']} failed" if outcome["failed"] else "")
        + ". " + "; ".join(outcome["lines"])
    )
    log.info("repricing_run_done", owner_user_id=owner_user_id, mode=mode, **{k: v for k, v in outcome.items() if k != "lines"})
    return state.write(owner_user_id, "done", KIND, scope=scope, ok=outcome["failed"] == 0, message=summary,
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
