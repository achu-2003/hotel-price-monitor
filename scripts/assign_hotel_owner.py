"""Give a hotel an owner, so an account can see it again.

Hotels are per-account from the 0006 migration onward: every screen filters on
``hotels.owner_user_id``, and a hotel with a NULL owner is visible to nobody.
Hotels that predate the column are in exactly that state.

This is the way back for one that should not have been left there -- the
counterpart to scripts/purge_unowned_hotels.py, for when the answer is "that
one is mine" rather than "delete it".

    python scripts/assign_hotel_owner.py --username AGS@123 --list
    python scripts/assign_hotel_owner.py --username AGS@123 --hotel-id 7
    python scripts/assign_hotel_owner.py --username AGS@123 --all-unowned

Reassigning a hotel that already has an owner needs --force. Without it, a
mistyped id would quietly move a property out of somebody else's dashboard and
into yours, and neither of you would be told.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.models import Hotel, User  # noqa: E402
from app.db.session import sync_session  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="Account to assign to")
    parser.add_argument("--hotel-id", type=int, action="append", default=[],
                        help="Hotel to assign. Repeatable.")
    parser.add_argument("--all-unowned", action="store_true",
                        help="Assign every hotel that currently has no owner")
    parser.add_argument("--list", action="store_true",
                        help="Show hotels and their owners, then exit")
    parser.add_argument("--force", action="store_true",
                        help="Allow taking a hotel that already has an owner")
    args = parser.parse_args()

    username = args.username.strip().lower()

    with sync_session() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            print(f"No account named {username!r}.", file=sys.stderr)
            return 2

        if args.list:
            _print_inventory(session)
            return 0

        if not args.hotel_id and not args.all_unowned:
            print("Nothing to do. Pass --hotel-id, --all-unowned, or --list.",
                  file=sys.stderr)
            return 2

        targets: list[Hotel] = []
        if args.all_unowned:
            targets.extend(
                session.scalars(
                    select(Hotel).where(Hotel.owner_user_id.is_(None)).order_by(Hotel.id)
                ).all()
            )
        for hotel_id in args.hotel_id:
            hotel = session.get(Hotel, hotel_id)
            if hotel is None:
                print(f"Hotel {hotel_id} does not exist.", file=sys.stderr)
                return 2
            if hotel not in targets:
                targets.append(hotel)

        if not targets:
            print("No matching hotels. Nothing to do.")
            return 0

        taken = [h for h in targets if h.owner_user_id not in (None, user.id)]
        if taken and not args.force:
            print(
                "These hotels already belong to another account:\n"
                + "\n".join(f"  [{h.id}] {h.name} (owner {h.owner_user_id})" for h in taken)
                + "\nRe-run with --force to take them.",
                file=sys.stderr,
            )
            return 2

        moved = 0
        for hotel in targets:
            if hotel.owner_user_id == user.id:
                continue
            hotel.owner_user_id = user.id
            moved += 1
            print(f"  [{hotel.id}] {hotel.name} -> {username}")

        session.commit()
        print(f"\nAssigned {moved} hotel(s) to {username}.")
    return 0


def _print_inventory(session) -> None:
    rows = session.execute(
        select(Hotel.id, Hotel.name, Hotel.owner_user_id, User.username)
        .outerjoin(User, Hotel.owner_user_id == User.id)
        .order_by(Hotel.id)
    ).all()
    if not rows:
        print("No hotels.")
        return
    for hotel_id, name, owner_id, owner_name in rows:
        owner = owner_name if owner_id is not None else "— unowned, visible to nobody"
        print(f"  [{hotel_id:>4}] {name:<40} {owner}")


if __name__ == "__main__":
    raise SystemExit(main())
