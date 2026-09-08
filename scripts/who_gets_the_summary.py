"""Who would receive the market summary, and on which channels.

WHY THIS EXISTS
===============
"Is it configured correctly?" is not answered by looking at the Alerts page.
A number reaches the summary only if all of this holds at once:

    the recipient is active
    AND it is assigned to a hotel that moved -- or carries alerts_all_hotels
    AND that assignment is active
    AND the assignment's channels include whatsapp
    AND the move clears the assignment's own threshold

Five conditions across three tables, and every one of them fails silently:
nothing is written, nothing is logged, and the panel goes on saying the number
is configured. The only honest way to answer is to run the dispatcher's own
grouping and print what comes out, which is what this does.

It reads. Nothing is written and nothing is sent.

    python scripts/who_gets_the_summary.py
    python scripts/who_gets_the_summary.py --hours 24

With no ``--hours`` it uses the interval set on Settings, so it answers the
question about the message that is actually scheduled.
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

from sqlalchemy import select  # noqa: E402

from app.db.models import Hotel, PriceChange, Recipient  # noqa: E402
from app.db.session import sync_session  # noqa: E402
from app.notifications.digest import group_for_digest, passes_recipient_threshold  # noqa: E402
from app.services import monitoring as monitoring_service  # noqa: E402
from app.workers import tasks_notify  # noqa: E402


def _kind(recipient) -> str:
    """Alert number, or ordinary assigned recipient.

    They are the same table and the same row type -- ``alerts_all_hotels``
    doubles as coverage and as identity, see ``_alert_numbers_query`` in
    app/api/v1/notifications.py -- so nothing on screen distinguishes them
    unless it is said. Which matters here: the two are configured on different
    pages, and somebody checking "did my number get set up" is usually holding
    one of them in mind while looking at the other.

    An inactive row that still carries the flag is a number that was REMOVED
    from the Alerts page. Removal clears ``is_active`` and leaves the flag set
    on purpose, so it can still be named as what it was rather than reappearing
    as an anonymous recipient with no email and no hotels.
    """
    if getattr(recipient, "alerts_all_hotels", False):
        return ("(alert number, every hotel)" if recipient.is_active
                else "(alert number, REMOVED)")
    return "(assigned recipient)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=int,
        help="Look back this many hours instead of the configured interval.",
    )
    args = parser.parse_args()

    interval = monitoring_service.summary_interval_hours()
    now = datetime.now(UTC)

    if args.hours:
        window_start, window_end = now - timedelta(hours=args.hours), now
        label = f"the last {args.hours} hours"
    else:
        if interval <= 0:
            print("Market summary is OFF (Settings > Market summary = 0).")
            print("Nothing is scheduled. Pass --hours N to see who WOULD get one.\n")
        window_start, window_end = tasks_notify._last_closed_window(
            now, interval or 2
        )
        label = f"{window_start:%H:%M}-{window_end:%H:%M} UTC"

    print(f"Interval on Settings : {interval} h" + ("  (OFF)" if not interval else ""))
    print(f"Window examined      : {label}\n")

    with sync_session() as session:
        changes = session.scalars(
            select(PriceChange).where(
                PriceChange.changed_at >= window_start,
                PriceChange.changed_at < window_end,
            )
        ).all()
        print(f"Price changes in it  : {len(changes)}")
        if not changes:
            print("\nNothing moved, so no message would be sent. That is not a")
            print("configuration problem -- a quiet window is deliberately silent.")
            return 0

        hotel_ids = {c.hotel_id for c in changes}
        hotels = {
            h.id: h
            for h in session.scalars(select(Hotel).where(Hotel.id.in_(hotel_ids)))
        }
        assignments = tasks_notify._assignments_for(session, hotel_ids)
        links = tasks_notify._links_by_pair(session, hotel_ids)
        facts = {c.id: tasks_notify._facts_for(c) for c in changes}

        everyone = session.scalars(select(Recipient).order_by(Recipient.id)).all()
        active = {r.id: r for r in everyone if r.is_active}

        # Exactly the grouping the task performs.
        reach: dict[int, dict[str, set[int]]] = {}
        for (recipient_id, hotel_id), batch in group_for_digest(
            list(facts.values()), assignments
        ).items():
            recipient = active.get(recipient_id)
            link = links.get((hotel_id, recipient_id))
            if recipient is None or link is None or not link.is_active:
                continue
            kept = [
                cid
                for cid in batch
                if passes_recipient_threshold(
                    facts[cid], link.min_delta_abs, link.min_delta_pct
                )
            ]
            if not kept:
                continue
            entry = reach.setdefault(recipient_id, {"channels": set(), "changes": set()})
            entry["channels"].update(link.channels or ["email"])
            entry["changes"].update(kept)

        print(f"Properties that moved: "
              f"{', '.join(sorted(h.name.strip() for h in hotels.values()))}\n")

        print("WOULD RECEIVE A SUMMARY")
        print("-" * 66)
        if not reach:
            print("  nobody")
        for recipient_id, entry in sorted(reach.items()):
            r = active[recipient_id]
            channels = sorted(entry["channels"])
            where = []
            if "whatsapp" in channels:
                where.append(r.phone_e164 or "NO PHONE NUMBER ON FILE")
            if "email" in channels:
                where.append(r.email or "no email on file")
            print(f"  {r.name!r}  {_kind(r)}")
            print(f"      channels : {', '.join(channels)}")
            print(f"      goes to  : {'  |  '.join(where)}")
            print(f"      covering : {len(entry['changes'])} of {len(changes)} changes")
            if "whatsapp" in channels and not r.phone_e164:
                print("      PROBLEM  : whatsapp is on but there is no number to send to")

        silent = [r for r in everyone if r.id not in reach]
        if silent:
            print("\nWOULD HEAR NOTHING, AND WHY")
            print("-" * 66)
            for r in silent:
                if not r.is_active:
                    why = "recipient is switched off"
                elif r.id not in {
                    rid for ids in assignments.values() for rid in ids
                }:
                    why = "not assigned to any hotel that moved, and not an alert number"
                else:
                    why = "assigned, but no move cleared their threshold"
                print(f"  {r.name!r:<26} {_kind(r):<28} {why}")

    print("\nNothing was sent. This command only reads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
