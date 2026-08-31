"""Turn the WhatsApp channel on (or off) for existing hotel assignments.

Which channels a person gets for a hotel lives in ``hotel_recipients.channels``,
one array per (hotel, recipient) pair. The dashboard edits one pair at a time,
which is right for a considered change and wrong for "switch WhatsApp on for
everything I already monitor" -- that is one round trip per hotel, and the
failure mode is stopping half way and believing it is done.

    python scripts/enable_whatsapp.py --list
    python scripts/enable_whatsapp.py --all
    python scripts/enable_whatsapp.py --recipient 1
    python scripts/enable_whatsapp.py --all --off

``--list`` changes nothing and is the sane first move: it shows every
assignment, its current channels, and whether the recipient actually has a
number to reach.

Email is never removed. This adds a channel alongside it rather than replacing
it, because losing the email trail is not what anybody means by "also send me a
WhatsApp".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.db.models import Hotel, HotelRecipient, Recipient  # noqa: E402
from app.db.session import sync_session  # noqa: E402
from app.notifications import registry  # noqa: E402

CHANNEL = "whatsapp"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="Show assignments and their channels, then exit")
    parser.add_argument("--all", action="store_true",
                        help="Every active assignment whose recipient has a phone number")
    parser.add_argument("--recipient", type=int, action="append", default=[],
                        help="Only this recipient id. Repeatable.")
    parser.add_argument("--off", action="store_true",
                        help="Remove the whatsapp channel instead of adding it")
    args = parser.parse_args()

    with sync_session() as session:
        if args.list:
            _print_inventory(session)
            return 0

        if not args.all and not args.recipient:
            print("Nothing to do. Pass --all, --recipient, or --list.", file=sys.stderr)
            return 2

        # Said out loud rather than enforced. Pre-staging the channel before the
        # credentials land is reasonable; believing messages are going out when
        # the provider is not configured is not.
        if not args.off and CHANNEL not in registry.available_channels():
            print(
                "WARNING: the whatsapp provider is not configured, so nothing will\n"
                "         actually send yet. Fill WHATSAPP_ENABLED / _PHONE_NUMBER_ID /\n"
                "         _ACCESS_TOKEN in .env and restart the API and the worker.\n",
                file=sys.stderr,
            )

        links = _targets(session, recipient_ids=args.recipient)
        if not links:
            print("No matching assignments. Nothing to do.")
            return 0

        changed = 0
        for link, hotel_name, recipient in links:
            channels = list(link.channels or [])

            if args.off:
                if CHANNEL not in channels:
                    continue
                channels.remove(CHANNEL)
            else:
                if CHANNEL in channels:
                    continue
                # Mirrors the check the API makes: WhatsApp needs an E.164
                # number, and an assignment without one produces a notification
                # row per price change that can only ever fail.
                if not recipient.phone_e164:
                    print(f"  skipped [{link.hotel_id}] {hotel_name}"
                          f" -- {recipient.name} has no phone number")
                    continue
                channels.append(CHANNEL)

            link.channels = channels
            # The column is a plain ARRAY, so SQLAlchemy does not see an in-place
            # edit. Reassigning covers it; the flag makes that explicit and
            # survives someone later switching to .append().
            flag_modified(link, "channels")
            changed += 1
            print(f"  [{link.hotel_id}] {hotel_name} -> {', '.join(channels)}")

        session.commit()
        verb = "Removed from" if args.off else "Added to"
        print(f"\n{verb} {changed} assignment(s).")
    return 0


def _targets(session, *, recipient_ids: list[int]):
    statement = (
        select(HotelRecipient, Hotel.name, Recipient)
        .join(Hotel, HotelRecipient.hotel_id == Hotel.id)
        .join(Recipient, HotelRecipient.recipient_id == Recipient.id)
        .where(HotelRecipient.is_active.is_(True), Recipient.is_active.is_(True))
        .order_by(Hotel.name)
    )
    if recipient_ids:
        statement = statement.where(HotelRecipient.recipient_id.in_(recipient_ids))
    return session.execute(statement).all()


def _print_inventory(session) -> None:
    rows = session.execute(
        select(HotelRecipient, Hotel.name, Recipient)
        .join(Hotel, HotelRecipient.hotel_id == Hotel.id)
        .join(Recipient, HotelRecipient.recipient_id == Recipient.id)
        .order_by(Recipient.name, Hotel.name)
    ).all()

    if not rows:
        print("No assignments. Nobody is told about anything.")
        return

    ready = registry.available_channels()
    print(f"Configured channels on this deployment: {', '.join(ready) or 'none'}\n")

    for link, hotel_name, recipient in rows:
        reach = recipient.phone_e164 or "no phone number"
        active = "" if link.is_active else "  (assignment inactive)"
        print(f"  [{link.hotel_id:>3}] {hotel_name}")
        print(f"        {recipient.name} <{reach}>"
              f"  channels: {', '.join(link.channels or []) or 'none'}{active}")


if __name__ == "__main__":
    raise SystemExit(main())
