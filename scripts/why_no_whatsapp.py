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

It reads. Nothing is written and nothing is sent.

    python scripts/why_no_whatsapp.py ags
    python scripts/why_no_whatsapp.py ags --days 30
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The Windows Server console is cp1252 and every rate carries a rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    AuditLog,
    Hotel,
    HotelRecipient,
    Notification,
    PriceChange,
    Recipient,
    User,
)
from app.db.session import sync_session  # noqa: E402
from app.services import monitoring as monitoring_service  # noqa: E402


def _flag(ok: bool) -> str:
    return "ok     " if ok else "BLOCKED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hotel", help="Part of the hotel name, e.g. ags")
    parser.add_argument("--days", type=int, default=14,
                        help="How far back to look for price changes (default 14).")
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
            print(f"     {row.at:%Y-%m-%d %H:%M:%S %Z}  by {username or row.user_id}"
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
                print(f"  [ok     ] {r.name}: alert number (every hotel)")

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

            last = session.scalars(
                select(Notification)
                .where(Notification.hotel_id == hotel.id,
                       Notification.channel == "whatsapp")
                .order_by(Notification.created_at.desc())
                .limit(3)
            ).all()
            if not last:
                print("  No WhatsApp notification has ever been written for this hotel")
            for n in last:
                err = f" {n.error_code}" if n.error_code else ""
                print(f"  WhatsApp {n.created_at:%Y-%m-%d %H:%M %Z}: {n.status}{err}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
