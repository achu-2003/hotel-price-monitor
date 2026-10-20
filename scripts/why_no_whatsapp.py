"""Why a hotel's price-change WhatsApp did not go out.

WHY THIS EXISTS
===============
A price-change WhatsApp is only written when every one of these holds:

    WHATSAPP_ENABLED=true in .env (read when the workers START)
    AND the WhatsApp switch on Settings is on
    AND the hotel has an active assignment whose channels include whatsapp
    AND the move is a price move that clears the assignment's threshold

The first three fail without writing a single notification row, so the
Alerts page shows nothing at all -- not even a failure. This prints each one
for one hotel, plus who flipped the Settings switch and when, from the audit
log, which no dashboard page shows.

No price move means no message, so it also prints when each of the hotel's
booking sites was last checked: a site that stopped being checked looks
exactly like a hotel whose rates never changed.

Each recent WhatsApp is also looked up at Sales Daddy, because "sent" here
only means the provider accepted it; whether it reached the phone is only
known there.

Times are printed in the app's timezone (Asia/Kolkata), not the server
clock's -- the Windows Server runs on UTC+13.

It reads. Nothing is written and nothing is sent.

    python scripts/why_no_whatsapp.py ags
    python scripts/why_no_whatsapp.py ags --days 30
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The Windows Server console is cp1252 and every rate carries a rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    AuditLog,
    CheckRun,
    Hotel,
    HotelRecipient,
    HotelSource,
    MonitorTarget,
    Notification,
    PriceChange,
    Recipient,
    Source,
    User,
)
from app.db.session import sync_session  # noqa: E402
from app.services import monitoring as monitoring_service  # noqa: E402


def _flag(ok: bool) -> str:
    return "ok     " if ok else "BLOCKED"


def _provider_status(message_id: str) -> str:
    """What Sales Daddy says happened to a message after it accepted it.

    "sent" on our side only means the provider answered 200. A lapsed
    subscription, a Marketing template or a number that is not on WhatsApp
    all fail AFTER that, and only this lookup sees it. A GET: nothing is sent.
    """
    settings = get_settings()
    if settings.whatsapp_provider != "salesdaddy" or not settings.salesdaddy_api_key:
        return "(not looked up: provider is not Sales Daddy)"
    url = f"{settings.salesdaddy_base_url.rstrip('/')}/v1/wa/messages/{message_id}"
    try:
        response = httpx.get(
            url, timeout=15,
            headers={"X-Api-Key": settings.salesdaddy_api_key.get_secret_value()},
        )
    except httpx.HTTPError as exc:
        return f"lookup failed: {exc.__class__.__name__}"
    if response.status_code != 200:
        return f"lookup failed: HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return "lookup failed: not JSON"
    data = body.get("data", body) if isinstance(body, dict) else {}
    status = data.get("status", "?")
    error = data.get("error")
    return f"{status}" + (f" -- {error}" if error else "")


def _at(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    return f"{moment.astimezone(ZoneInfo(get_settings().timezone)):%Y-%m-%d %H:%M} IST"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hotel", help="Part of the hotel name, e.g. ags")
    parser.add_argument("--days", type=int, default=14,
                        help="How far back to look for price changes (default 14).")
    parser.add_argument("--messages", type=int, default=6,
                        help="How many recent WhatsApp messages to look up (default 6).")
    args = parser.parse_args()

    settings = get_settings()
    since = datetime.now(UTC) - timedelta(days=args.days)

    with sync_session() as session:
        hotels = session.scalars(
            select(Hotel).where(Hotel.name.ilike(f"%{args.hotel.strip()}%"))
        ).all()
        if not hotels:
            # Names are typed by hand: "AGS" is saved as "ASG" on one server.
            print(f"No hotel name contains {args.hotel!r}. Hotels are:")
            for h in session.scalars(select(Hotel).order_by(Hotel.id)):
                print(f"  {h.id:>3}  {h.name}")
            return 1

        env_ok = bool(settings.whatsapp_enabled)
        key_ok = bool(settings.salesdaddy_api_key) if settings.whatsapp_provider == "salesdaddy" else True
        switch_ok = monitoring_service.whatsapp_alerts_enabled()

        print("DEPLOYMENT-WIDE")
        print(f"  [{_flag(env_ok)}] .env WHATSAPP_ENABLED = {settings.whatsapp_enabled}"
              "  (as read by THIS process; restart the services after editing)")
        print(f"  [{_flag(key_ok)}] provider {settings.whatsapp_provider}, "
              f"template {settings.whatsapp_template_name or '(none)'}")
        print(f"  [{_flag(switch_ok)}] Settings > WhatsApp alerts switch = "
              f"{'ON' if switch_ok else 'OFF'}")

        flips = session.execute(
            select(AuditLog, User.username)
            .outerjoin(User, User.id == AuditLog.user_id)
            .where(AuditLog.entity == "alert_defaults")
            .order_by(AuditLog.at.desc())
            .limit(40)
        ).all()
        shown = 0
        for row, username in flips:
            before = (row.before or {}).get("whatsapp_alerts_enabled")
            after = (row.after or {}).get("whatsapp_alerts_enabled")
            if before == after:
                continue
            if shown == 0:
                print("  Last changes to that switch (newest first):")
            print(f"     {_at(row.at)}  by {username or row.user_id}"
                  f"  {before} -> {after}")
            shown += 1
            if shown == 5:
                break
        if shown == 0:
            print("  (no recorded change to that switch)")

        for hotel in hotels:
            print(f"\nHOTEL {hotel.id}: {hotel.name}")
            links = session.execute(
                select(HotelRecipient, Recipient)
                .join(Recipient, Recipient.id == HotelRecipient.recipient_id)
                .where(HotelRecipient.hotel_id == hotel.id)
            ).all()
            alert_numbers = session.scalars(
                select(Recipient).where(Recipient.alerts_all_hotels.is_(True),
                                        Recipient.is_active.is_(True))
            ).all()
            if not links and not alert_numbers:
                print("  [BLOCKED] nobody is assigned to this hotel")
            for link, r in links:
                channels = link.channels or ["email"]
                wa = "whatsapp" in channels
                live = link.is_active and r.is_active
                print(f"  [{_flag(wa and live and bool(r.phone_e164))}] {r.name}: "
                      f"channels={channels} link_active={link.is_active} "
                      f"recipient_active={r.is_active} phone={'yes' if r.phone_e164 else 'NONE'} "
                      f"min_abs={link.min_delta_abs} min_pct={link.min_delta_pct}")
            for r in alert_numbers:
                tail = f"...{r.phone_e164[-4:]}" if r.phone_e164 else "NONE"
                print(f"  [ok     ] {r.name}: alert number (every hotel), recipient {r.id}, "
                      f"phone {tail}")

            day = func.date(PriceChange.changed_at)
            moves = session.execute(
                select(day, func.count())
                .where(PriceChange.hotel_id == hotel.id,
                       PriceChange.changed_at >= since,
                       PriceChange.suppressed_reason.is_(None))
                .group_by(day).order_by(day)
            ).all()
            total = sum(n for _, n in moves)
            print(f"  Price moves to alert on, last {args.days} days: {total}"
                  + (f"  (latest {moves[-1][0]})" if moves else ""))

            print("  Price checks per booking site:")
            sites = session.execute(
                select(HotelSource, Source)
                .join(Source, Source.id == HotelSource.source_id)
                .where(HotelSource.hotel_id == hotel.id)
                .order_by(HotelSource.id)
            ).all()
            if not sites:
                print("     [BLOCKED] no booking site attached, so nothing is checked")
            day_ago = datetime.now(UTC) - timedelta(hours=24)
            for hs, source in sites:
                targets = session.scalars(
                    select(MonitorTarget).where(MonitorTarget.hotel_source_id == hs.id)
                ).all()
                if not targets:
                    print(f"     [BLOCKED] {source.display_name}: no monitor target")
                for t in targets:
                    runs_24h = session.scalar(
                        select(func.count()).select_from(CheckRun)
                        .where(CheckRun.monitor_target_id == t.id,
                               CheckRun.started_at >= day_ago)
                    )
                    latest = session.scalars(
                        select(CheckRun).where(CheckRun.monitor_target_id == t.id)
                        .order_by(CheckRun.started_at.desc()).limit(1)
                    ).first()
                    live = hs.is_active and t.is_enabled and source.is_enabled
                    fresh = t.last_success_at is not None and t.last_success_at >= day_ago
                    print(f"     [{_flag(live and fresh)}] {source.display_name} "
                          f"(target {t.id}): site_active={hs.is_active} "
                          f"target_enabled={t.is_enabled} source_enabled={source.is_enabled} "
                          f"circuit={t.circuit_state.value}")
                    print(f"         last success {_at(t.last_success_at)}, "
                          f"last failure {_at(t.last_failure_at)}, "
                          f"{t.consecutive_failures} failures in a row, "
                          f"{runs_24h} checks in 24h, next {_at(t.next_run_at)}")
                    if latest is not None:
                        err = f" -- {latest.error_summary[:160]}" if latest.error_summary else ""
                        print(f"         latest check {_at(latest.started_at)}: "
                              f"{latest.status.value}, {latest.offers_found} offers, "
                              f"{latest.changes_detected} changes{err}")

            last = session.execute(
                select(Notification, Recipient)
                .outerjoin(Recipient, Recipient.id == Notification.recipient_id)
                .where(Notification.hotel_id == hotel.id,
                       Notification.channel == "whatsapp")
                .order_by(Notification.created_at.desc())
                .limit(args.messages)
            ).all()
            if not last:
                print("  No WhatsApp notification has ever been written for this hotel")
            else:
                print("  Latest WhatsApp messages (ours -> what Sales Daddy says now):")
            for n, r in last:
                who = r.name if r else f"recipient {n.recipient_id}"
                tail = f"...{r.phone_e164[-4:]}" if r and r.phone_e164 else "no phone"
                err = f" {n.error_code}" if n.error_code else ""
                print(f"     {_at(n.created_at)}  to {who} ({tail}): {n.status}{err}")
                if n.provider_message_id:
                    print(f"         Sales Daddy: {_provider_status(n.provider_message_id)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
