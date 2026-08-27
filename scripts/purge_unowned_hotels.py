"""Erase every hotel that no account owns, and everything collected for it.

After the 0006 migration, hotels added before ownership existed have a NULL
``owner_user_id``. They are invisible on every screen, but they are still in
the database and their targets are still being checked -- the workers do not
filter on owner, by design, so a hotel keeps collecting whether or not anyone
can currently see it.

This is how that state ends when the answer is "delete them", rather than
"adopt them" (scripts/assign_hotel_owner.py).

NOT REVERSIBLE
==============
Same destruction as ``POST /hotels/{id}/purge``: the hotel row, and with it
every source, room type, alias, monitor target, price series, price change,
monitoring error and recipient link that hangs off it by ON DELETE CASCADE --
plus the price observations, which the cascade cannot reach.

So it refuses to do anything until asked twice. With no flags it prints what
it WOULD delete and exits; ``--yes`` is what actually deletes. Read the
dry-run output first: there is no undo inside this application, only a
database restore.

    python scripts/purge_unowned_hotels.py            # show me
    python scripts/purge_unowned_hotels.py --yes      # do it

OBSERVATIONS ARE DELETED BY KEY, NOT BY CASCADE
===============================================
``price_observations`` has no ``hotel_id`` -- it is keyed by ``offer_key``,
which is what makes the table cheap to write. Skipping this step would leave
rows that no query can ever return and no retention policy will ever prune.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db.models import (  # noqa: E402
    AuditLog,
    Hotel,
    PriceChange,
    PriceObservation,
    PriceSeries,
)
from app.db.session import sync_session  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this the script only reports.",
    )
    args = parser.parse_args()

    with sync_session() as session:
        hotels = list(
            session.scalars(
                select(Hotel).where(Hotel.owner_user_id.is_(None)).order_by(Hotel.id)
            ).all()
        )

        if not hotels:
            # Still worth a look at the observations: a previous run of this
            # script, or a purge through the API, can clear every unowned
            # hotel and leave orphans behind from before it existed.
            print("No unowned hotels.")
            stranded = _count_orphaned_observations(session)
            if not stranded:
                print("No orphaned observations either. Nothing to do.")
                return 0
            if not args.yes:
                print(
                    f"\n{stranded} orphaned observation(s) remain — rows whose "
                    f"price series is already gone.\n"
                    f"DRY RUN — nothing was deleted. Re-run with --yes to sweep them."
                )
                return 0
            swept = _sweep_orphaned_observations(session)
            session.commit()
            print(f"Swept {swept} orphaned observation(s).")
            return 0

        planned = [_survey(session, hotel) for hotel in hotels]

        print(f"{len(planned)} unowned hotel(s):\n")
        for row in planned:
            print(
                f"  [{row['id']:>4}] {row['name']}\n"
                f"         {row['series']} price series, "
                f"{row['observations']} observations, {row['changes']} changes"
            )
        totals = {
            key: sum(row[key] for row in planned)
            for key in ("series", "observations", "changes")
        }
        print(
            f"\nTotal: {len(planned)} hotels, {totals['series']} series, "
            f"{totals['observations']} observations, {totals['changes']} changes."
        )

        if not args.yes:
            print(
                "\nDRY RUN — nothing was deleted.\n"
                "Re-run with --yes to delete all of the above permanently."
            )
            return 0

        for row in planned:
            _purge(session, row)

        orphans = _sweep_orphaned_observations(session)

        # One transaction: either every hotel goes or none does. A partial
        # purge interrupted halfway would leave observations orphaned from
        # series that are already gone, which nothing afterwards can find.
        session.commit()

        print(
            f"\nDeleted {len(planned)} hotels, {totals['series']} series, "
            f"{totals['observations']} observations, {totals['changes']} changes."
        )
        if orphans:
            print(
                f"Also swept {orphans} observation(s) already orphaned before "
                f"this run — see _sweep_orphaned_observations."
            )
    return 0


def _orphaned_observations():
    """The WHERE clause for "this observation's series is gone"."""
    return ~select(PriceSeries.offer_key).where(
        PriceSeries.offer_key == PriceObservation.offer_key
    ).exists()


def _count_orphaned_observations(session) -> int:
    return session.scalar(
        select(func.count()).select_from(PriceObservation).where(_orphaned_observations())
    ) or 0


def _sweep_orphaned_observations(session) -> int:
    """Delete observations whose price series no longer exists.

    ``price_observations`` is keyed by ``offer_key`` and has no ``hotel_id``,
    which is what makes it cheap to write and what puts it outside every
    ON DELETE CASCADE in the schema. So anything that removed a
    ``price_series`` row WITHOUT also deleting by key -- an earlier hard
    delete, a series reset after a URL was repointed -- left its observations
    behind.

    Those rows are unreachable: no series, no hotel, no query that can return
    them and no retention policy that will ever prune them. Counted and
    reported separately rather than folded into the per-hotel numbers above,
    because they are not attributable to any hotel on that list.
    """
    result = session.execute(delete(PriceObservation).where(_orphaned_observations()))
    return result.rowcount or 0


def _survey(session, hotel: Hotel) -> dict:
    """Count what deleting this hotel would destroy, before destroying it."""
    offer_keys = list(
        session.scalars(
            select(PriceSeries.offer_key).where(PriceSeries.hotel_id == hotel.id)
        ).all()
    )
    observations = 0
    if offer_keys:
        observations = session.scalar(
            select(func.count(PriceObservation.offer_key)).where(
                PriceObservation.offer_key.in_(offer_keys)
            )
        ) or 0
    changes = session.scalar(
        select(func.count(PriceChange.id)).where(PriceChange.hotel_id == hotel.id)
    ) or 0
    return {
        "id": hotel.id,
        "name": hotel.name,
        "slug": hotel.slug,
        "offer_keys": offer_keys,
        "series": len(offer_keys),
        "observations": observations,
        "changes": changes,
    }


def _purge(session, row: dict) -> None:
    if row["offer_keys"]:
        session.execute(
            delete(PriceObservation).where(
                PriceObservation.offer_key.in_(row["offer_keys"])
            )
        )

    # Written BEFORE the row goes, so the trail keeps the name and the numbers
    # after there is nothing left to look up. user_id is NULL: this ran from a
    # shell, and inventing an operator here would be a worse record than
    # admitting there wasn't one.
    session.add(
        AuditLog(
            user_id=None,
            action="purge",
            entity="hotel",
            entity_id=str(row["id"]),
            before={"name": row["name"], "slug": row["slug"]},
            after={
                "series": row["series"],
                "observations": row["observations"],
                "changes": row["changes"],
                "reason": "unowned",
                "via": "scripts/purge_unowned_hotels.py",
            },
            at=datetime.now(UTC),
        )
    )

    # A Core DELETE, not session.delete(): the database's own ON DELETE CASCADE
    # covers sources, rooms, targets, series, changes, errors and recipient
    # links without the ORM lazy-loading every relationship to walk it.
    session.execute(delete(Hotel).where(Hotel.id == row["id"]))


if __name__ == "__main__":
    raise SystemExit(main())
