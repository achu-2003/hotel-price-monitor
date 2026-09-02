"""Re-deriving a source's selectors when the stored ones stop describing the page.

This is the task half of :mod:`app.services.rediscovery`; the rules it obeys
and the reasoning behind them are documented there.

WHY A SEPARATE TASK
===================
It drives a real browser, so it belongs on the ``browser`` queue alongside the
fetches and away from the light HTTP work. It is also deliberately decoupled
from the fetch that triggered it: repairing a config must never delay, fail, or
roll back the price data that exposed the problem. The fetch commits its
observations, notices something is wrong, and hands the repair off.

A failure in here is contained the same way ``fetch_prices`` contains its own:
the source keeps the configuration it had, the operator alert stays unresolved,
and the next fetch behaves exactly as it would have done. The worst outcome of
this task is that nothing happens.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from celery import shared_task
from sqlalchemy import select

from app.adapters.discovery import json_fragment
from app.adapters.mapping import render_template
from app.config import get_settings
from app.core.logging import get_logger
from app.db.models import AuditLog, HotelSource, MonitoringError
from app.db.session import sync_session
from app.services.dates import local_today
from app.services.rediscovery import (
    DISCOVERY_VERSION,
    REPAIRABLE,
    VERSION_KEY,
    RepairState,
    identity_selectors_changed,
    is_a_real_change,
    may_attempt,
    merge_config,
    names_to_retire,
)

log = get_logger("tasks.repair")


@shared_task(name="repair.rediscover_source", ignore_result=True)
def rediscover_source(
    hotel_source_id: int,
    *,
    check_in: str | None = None,
    check_out: str | None = None,
    adults: int = 2,
    children: int = 0,
    reason: str = "",
    collapsed_names: list[str] | None = None,
) -> dict[str, Any]:
    """Re-run discovery against a live source and store the result if it verifies.

    Returns a small dict describing what happened, which is what the tests
    assert on and what appears in the worker log.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    logger = log.bind(hotel_source_id=hotel_source_id, reason=reason or "unspecified")

    # ── 0. refuse a window that has already begun ────────────────────
    # The stay is frozen into this task's kwargs at enqueue time so that
    # discovery inspects the page the fetch actually read. That is right while
    # the message is fresh and wrong once it is not: a booking site asked for a
    # past date renders no rooms, and discovery would then either find nothing
    # or, worse, derive selectors from an empty-state page and store them as
    # the repair.
    #
    # Checked BEFORE the claim below, so a backlog of stale repairs cannot
    # spend the attempt budget that the real ones need. Skipping costs nothing:
    # the config is untouched, the operator alert stands, and the next fetch
    # asks again with a window that means something.
    if check_in and date.fromisoformat(check_in) < local_today(settings.timezone):
        logger.info("rediscovery_skipped", why="stay window already started", check_in=check_in)
        return {"status": "skipped", "why": "stale window"}

    # ── 1. claim the attempt ─────────────────────────────────────────
    # Written and COMMITTED before the browser runs. Discovery takes tens of
    # seconds and can die outright; recording the attempt afterwards would mean
    # a crash left the cooldown untouched and the next trigger started another
    # one immediately.
    with sync_session() as session:
        source_row = session.execute(
            select(HotelSource)
            .where(HotelSource.id == hotel_source_id)
            .with_for_update()
        ).scalar_one_or_none()

        if source_row is None:
            logger.info("rediscovery_skipped", why="hotel source no longer exists")
            return {"status": "skipped", "why": "missing"}

        config = dict(source_row.adapter_config or {})
        state = RepairState.from_config(config)
        verdict = may_attempt(
            state,
            now=now,
            enabled=settings.auto_rediscovery_enabled,
            cooldown_minutes=settings.auto_rediscovery_cooldown_minutes,
            max_attempts=settings.auto_rediscovery_max_attempts,
        )
        if not verdict.allowed:
            logger.info("rediscovery_skipped", why=verdict.reason)
            return {"status": "skipped", "why": verdict.reason}

        template = source_row.url
        hotel_id = source_row.hotel_id
        currency = source_row.currency

        # The claim itself. The outcome is overwritten below; recording it as
        # "started" means a task killed mid-flight still leaves a truthful
        # trail rather than looking like it never ran.
        source_row.adapter_config = {
            **config,
            **state.claim(now),
        }
        session.commit()

    if not template:
        _finish(hotel_source_id, outcome="no_url", now=now)
        logger.info("rediscovery_skipped", why="source has no url to inspect")
        return {"status": "skipped", "why": "no url"}

    url = render_template(
        template,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
        rooms=1,
        nights=1,
        currency=currency,
    )

    # ── 2. inspect the live page ─────────────────────────────────────
    # Imported here rather than at module scope: it pulls in Playwright, and a
    # web process importing this module for the task name should not pay for a
    # browser stack it will never start.
    from app.adapters.discovery import inspect_url

    try:
        result = inspect_url(url, check_in=check_in, check_out=check_out, adults=adults)
    except Exception as exc:  # noqa: BLE001 - a repair must not raise past here
        _finish(hotel_source_id, outcome="error", now=now)
        logger.warning("rediscovery_failed", error=str(exc)[:200])
        return {"status": "failed", "why": str(exc)[:200]}

    # ── 3. decide ────────────────────────────────────────────────────
    # ``is_strongly_verified`` rather than ``result.ok``.
    #
    # A repair is the one place discovery writes to live configuration with
    # nobody reading the result, so it answers to a higher bar than the same
    # finding shown to the person who pasted the URL: at least one of the
    # prices has to have been printed with a currency beside it. A hotel sold
    # out for the night otherwise supplies a page with no rates on it, and
    # every remaining check will happily confirm that some number on that page
    # is a number on that page. See Candidate.is_strongly_verified.
    #
    # The cost of the higher bar is a repair that declines and leaves the
    # alert standing. That is the outcome this is for.
    if result.unlearnable:
        # Not a failed repair: there was nothing on the page to repair FROM.
        # The attempt is handed back so a hotel that sells out for a few
        # nights does not spend its budget on nights nobody could have read,
        # and arrive at "this needs a person" having never been looked at.
        _finish(hotel_source_id, outcome="unlearnable", now=now, refund=True)
        logger.info("rediscovery_unlearnable", why=result.unlearnable[:200])
        return {"status": "unlearnable", "why": result.unlearnable[:200]}

    if not result.ok or result.best is None or not result.best.is_strongly_verified:
        note = (result.notes[-1] if result.notes else "nothing usable was found")
        if result.ok and result.best is not None:
            note = (
                "found a room list, but not one of its prices was printed with "
                "a currency beside it, so none of them can be told from an "
                "ordinary number on the page"
            )
        _finish(hotel_source_id, outcome="unverified", now=now)
        logger.info("rediscovery_unverified", why=note[:200])
        return {"status": "unverified", "why": note[:200]}

    best = result.best
    fragment = json_fragment(best.source_url)
    discovered = best.as_adapter_config(fragment)
    discovered["discovery_note"] = (
        f"Auto-repaired {now.date().isoformat()}: {best.room_count} rooms, "
        f"{best.corroborated}/{len(best.sample_prices)} prices confirmed "
        f"against the page, {best.corroborated_marked} of them printed with a "
        f"currency."
    )

    with sync_session() as session:
        source_row = session.execute(
            select(HotelSource)
            .where(HotelSource.id == hotel_source_id)
            .with_for_update()
        ).scalar_one_or_none()
        if source_row is None:  # pragma: no cover - deleted mid-repair
            return {"status": "skipped", "why": "missing"}

        current = dict(source_row.adapter_config or {})
        state = RepairState.from_config(current)

        if not is_a_real_change(current, discovered):
            # The page still matches what is stored, so whatever is wrong is
            # not the selectors. Leaving the alert open is the point: this is
            # the case where an automatic fix would have hidden the problem.
            #
            # The version stamp is written even though the config did not
            # change, and it has to be. "This generation looked and found
            # nothing to alter" is a real answer from the CURRENT scanner, and
            # recording it is what ends the attempt. Without the stamp the
            # source would still read as never-tried, the gate would wave the
            # next fetch straight through, and the repair would drive a browser
            # at somebody else's site every half hour forever -- the runaway
            # loop the attempt budget exists to prevent, reintroduced by the
            # mechanism meant to let a fixed scanner reach old configs.
            source_row.adapter_config = {
                **current,
                VERSION_KEY: DISCOVERY_VERSION,
                **state.settle(now, outcome="no_change"),
            }
            session.commit()
            logger.info("rediscovery_no_change",
                        note="stored selectors still match the page")
            return {"status": "no_change"}

        before = {k: v for k, v in current.items() if k != "auto_repair"}
        repaired = merge_config(current, discovered)
        # reset=True: this source is fixed, so a future break starts with a
        # full budget instead of inheriting today's spent one.
        repaired.update(state.settle(now, outcome="repaired", reset=True))
        source_row.adapter_config = repaired
        source_row.updated_at = now

        # The alert that started this can now be closed, but ONLY the kind this
        # task can actually fix. Resolving every open error for the hotel would
        # sweep away a blocked source or an expired certificate that nobody has
        # looked at.
        resolved = 0
        open_errors = session.scalars(
            select(MonitoringError).where(
                MonitoringError.hotel_id == hotel_id,
                MonitoringError.resolved_at.is_(None),
            )
        ).all()
        for error in open_errors:
            if str(error.error_class.value) in REPAIRABLE:
                error.resolved_at = now
                resolved += 1

        # Before the audit row is built, so what it records includes what was
        # deleted. This is the only irreversible thing the repair does -- the
        # price history of a retired room goes with it -- and an audit trail
        # that omitted it would leave no way to find out afterwards which rooms
        # a repair removed, or that it removed any.
        retired = _retire_invented_rooms(
            session,
            hotel_id=hotel_id,
            collapsed_names=collapsed_names,
            # The neighbours a "similar properties" carousel put in this
            # hotel's room list. Nothing in a fetch can report this -- the
            # prices are real and every check succeeds -- so the scan is the
            # only place the evidence exists.
            cross_sold_names=result.cross_sold_names,
            discovered_names=best.sample_names,
            logger=logger,
        )

        superseded = 0
        # Against what is being STORED, not against what was discovered: a
        # meal_plan a person set and this scan did not re-derive is carried
        # through by merge_config, and the offers keep the keys they had.
        if identity_selectors_changed(current, repaired):
            superseded = _retire_superseded_series(
                session,
                hotel_id=hotel_id,
                source_id=source_row.source_id,
                today=local_today(settings.timezone),
                logger=logger,
            )

        session.add(
            AuditLog(
                user_id=None,          # nobody: this was the system repairing itself
                action="auto_rediscover",
                entity="hotel_source",
                entity_id=str(hotel_source_id),
                before=before,
                after={
                    **{k: v for k, v in repaired.items() if k != "auto_repair"},
                    "rooms_retired": retired,
                    "series_superseded": superseded,
                    "rooms_discovered": list(best.sample_names[:12]),
                },
                # No Python-side default on this column, and the audit write
                # shares the transaction with the repair -- omitting it fails
                # the flush and takes the repair down with it.
                at=now,
            )
        )

        session.commit()

    logger.info(
        "rediscovery_repaired",
        rooms=best.room_count,
        names=best.sample_names[:6],
        errors_resolved=resolved,
        rooms_retired=retired,
        series_superseded=superseded,
    )
    return {
        "status": "repaired",
        "rooms": best.room_count,
        "errors_resolved": resolved,
        "rooms_retired": retired,
        "series_superseded": superseded,
        "config": discovered,
    }


def _retire_invented_rooms(
    session,
    *,
    hotel_id: int,
    collapsed_names: list[str] | None,
    discovered_names: list[str],
    logger,
    cross_sold_names: list[str] | None = None,
) -> list[str]:
    """Delete the room types the broken config invented. See names_to_retire().

    DELETED rather than deactivated, which is the harsher of the two and the
    right one. Deactivating leaves the price series attached, and that series
    holds figures scraped from the wrong element — the dashboard would go on
    showing ₹169.50 as a current rate, and the next fetch, no longer finding
    the room, would raise it as SOLD OUT and notify somebody. Retiring the room
    but keeping its numbers turns a silent fault into a loud wrong one.

    Nothing genuine is at risk: only names the ingest layer watched collapse,
    and only those the repaired config does not read back.
    """
    from app.db.models import RoomType
    from app.services import room_matching

    retire = names_to_retire(collapsed_names, discovered_names, cross_sold_names)
    if not retire:
        return []

    canonical = {
        room_matching.normalize_room_name(name)
        for name in retire
        if room_matching.normalize_room_name(name)
    }
    if not canonical:
        return []

    rooms = session.scalars(
        select(RoomType).where(
            RoomType.hotel_id == hotel_id,
            RoomType.canonical_name.in_(canonical),
        )
    ).all()

    removed = []
    for room in rooms:
        removed.append(room.name)
        session.delete(room)   # cascades to its price series and aliases

    if removed:
        logger.info("invented_rooms_retired", rooms=removed[:8])
    return removed


def _retire_superseded_series(
    session,
    *,
    hotel_id: int,
    source_id: int,
    today: date,
    logger,
) -> int:
    """Drop the series the repaired config can no longer address.

    Called only when the repair changed a selector the OFFER KEY is built from
    — see :func:`identity_selectors_changed`. Every offer on the page now
    hashes to a key nothing has ever written, and the rows under the old keys
    are unreachable: no fetch will ever update one again.

    Left in place they do not sit quietly. ``_handle_disappearances`` selects
    every available series for the stay being checked, finds each of these
    absent from the fetch, and one check later records it as
    BECAME_UNAVAILABLE — so a repair that fixed a silently wrong price would
    announce that the hotel had sold out, room by room, to everybody on the
    recipient list.

    ONLY THE STAYS STILL AHEAD
    ==========================
    A series for a night already past is never selected again by that sweep,
    which filters on the stay being checked. It is inert, it is real history,
    and it is left exactly where it is. What has to go is the small set the
    monitor is still working on, and those are rebuilt under their new keys by
    the next fetch — within one interval, with the rate plan that was missing.

    DELETED rather than deactivated, for the reason ``_retire_invented_rooms``
    gives: a deactivated series keeps its last price on the dashboard, so the
    hotel would show every room twice — once live and correct, once frozen at
    whatever it cost the day the config changed.

    Room types are untouched. Only the price series is keyed on the plan; the
    rooms themselves are matched by name and are as valid after the repair as
    before it.
    """
    from app.db.models import PriceSeries

    doomed = session.scalars(
        select(PriceSeries).where(
            PriceSeries.hotel_id == hotel_id,
            PriceSeries.source_id == source_id,
            PriceSeries.check_in >= today,
        )
    ).all()

    for series in doomed:
        session.delete(series)

    if doomed:
        logger.info(
            "superseded_series_retired",
            count=len(doomed),
            why="the repair changed a selector the offer key is built from",
        )
    return len(doomed)


def _finish(
    hotel_source_id: int, *, outcome: str, now: datetime, refund: bool = False
) -> None:
    """Record how an attempt ended without touching the configuration itself.

    ``refund`` hands the attempt back, for an ending that proved nothing about
    whether this source can be repaired -- see ``RepairState.settle``.

    THE GENERATION STAMP GOES DOWN HERE TOO
    =======================================
    "This scanner looked and could not do it" is a real answer from the CURRENT
    scanner, and recording it is what ends the attempt -- the same reasoning the
    ``no_change`` branch above sets out. Without the stamp the source still
    reads as never-tried, ``scanner_moved_on`` waves it past the budget every
    time, and the sweep that looks for behind-generation configs finds it again
    on the next pass. A browser at somebody else's site every hour, forever:
    the runaway loop the budget exists to prevent, arriving through the
    mechanism meant to let a fixed scanner reach old configs.

    A REFUND IS THE EXCEPTION, and it is the same exception it always was.
    There the page had nothing on it to read -- a hotel sold out for the night
    -- so this generation never actually got to look, and the question it would
    have answered has not been asked. The cooldown paces that one instead.
    """
    with sync_session() as session:
        row = session.execute(
            select(HotelSource)
            .where(HotelSource.id == hotel_source_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover
            return
        config = dict(row.adapter_config or {})
        # settle(), not claim(): the attempt was already spent when it was
        # claimed. This only records how it ended.
        row.adapter_config = {
            **config,
            **({} if refund else {VERSION_KEY: DISCOVERY_VERSION}),
            **RepairState.from_config(config).settle(
                now, outcome=outcome, refund=refund
            ),
        }
        session.commit()


@shared_task(name="discover.inspect_url")
def inspect_pasted_url(
    url: str,
    *,
    check_in: str | None = None,
    check_out: str | None = None,
    adults: int = 2,
) -> dict[str, Any]:
    """Inspect a pasted booking URL in a browser, for the attach endpoint.

    WHY THIS IS A TASK AND NOT A FUNCTION CALL
    ==========================================
    Attaching a hotel on an unrecognised engine has to open the page in a real
    browser. The API cannot do that: docker/Dockerfile.api ships no browser on
    purpose -- it is what keeps that image at ~200MB rather than ~1.7GB, and
    what stops a Chromium leak reaching the web server and the scheduler. So
    calling discovery in-process worked natively, where one process has
    everything, and failed in Docker with

        BrowserType.launch: Executable doesn't exist at
        /home/appuser/.cache/ms-playwright/...

    which named a missing file rather than the missing design decision.

    ``repair.rediscover_source`` had already settled where this work belongs;
    this is the same journey for the other caller.

    RETURNS DATA, NOT EXCEPTIONS
    ============================
    Failures come back as a dict rather than propagating. Celery's JSON
    serializer can only re-raise an exception the caller can import and
    reconstruct, and the distinction that matters here -- a FetchError, which
    has already explained itself in a sentence an operator can act on, versus
    anything else, where only the first line is worth showing -- is exactly
    what would be lost in that round trip. So it is decided here, where the
    exception is still real, and travels as a flag.
    """
    from app.adapters.discovery import inspect_url
    from app.core.errors import FetchError

    try:
        result = inspect_url(
            url, check_in=check_in, check_out=check_out, adults=adults
        )
    except FetchError as exc:
        log.warning("discovery_failed", url=url[:120], error=str(exc))
        return {"ok": False, "raised": True, "explained": True, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported to the operator, never leaked raw
        log.warning("discovery_failed", url=url[:120], error=str(exc))
        # Playwright names every navigation failure "Error", so the class name
        # alone is a dead end. The first line is the reason; the rest is a call
        # log nobody needs in a form field.
        reason = str(exc).splitlines()[0].strip() or type(exc).__name__
        return {"ok": False, "raised": True, "explained": False, "message": reason}

    if not result.ok:
        return {
            "ok": False,
            "raised": False,
            "reason": result.notes[-1] if result.notes else "nothing usable was found.",
        }

    best = result.best
    fragment = json_fragment(best.source_url)
    return {
        "ok": True,
        "raised": False,
        "config": best.as_adapter_config(fragment),
        "fragment": fragment,
        "room_count": best.room_count,
        "corroborated": best.corroborated,
        "sample_count": len(best.sample_prices),
        "fields": best.fields,
    }
