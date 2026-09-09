"""Put a known-good config back on a source discovery cannot read.

WHEN THIS IS THE RIGHT TOOL
===========================
Almost never. A source whose selectors have gone stale is repaired by
discovery, and the repair is automatic precisely so that nobody has to do this.

The exception is a page discovery ranks WRONGLY rather than fails to read.
Treebo Emerald Dove, 9 Sep 2026, with both guards deployed and a fresh scan:

    dom_scan  best=div.cikLsc.kyiLCd  candidates=5  prices_on_page=16
    discovery_complete  rooms=1  verified=True
    rediscovery_named_after_property  names=['Treebo Premium Emerald Dove ...']

Sixteen prices on the page, five candidates, and the winner yields one "room"
which is the property heading. The guard declines it, correctly, and declining
is all it can do -- the stored config stays broken, and re-running discovery
reaches the same conclusion because nothing about the page or the ranking has
changed. Automatic repair has no move left. That is what this is for.

WHAT IT WRITES
==============
Not a hand-authored selector. ``--like-source`` copies the config off another
source that is demonstrably reading its page today, which keeps the thing being
restored something the system itself produced and something you can point at.

Prefer a source whose selectors are TEXT rather than hashed classes. Treebo's
class names are generated and rotate -- cikLsc, jptIKY, gCkgJO -- and a config
built on them is a config that will break again on the next deploy of their
front end. ``text=/Room \\(/`` matches the room wording and does not care.

THE INVENTED ROOM
=================
Restoring the config does not remove the room type the broken one created. It
sits in the database with its price series, the fetch no longer finds it, and
the pipeline reads that as SOLD OUT and tells somebody. ``--retire-named-after-
property`` removes exactly the room types that echo the hotel name, by the same
rule the repair guard uses.

That DELETES the room and its price history, which is not reversible. Nothing
genuine is at risk -- a room called what its hotel is called was never a room --
but it is shown to you before it happens and needs --yes.

    python scripts/restore_source_config.py --source-id 5 --like-source 7
    python scripts/restore_source_config.py --source-id 5 --like-source 7 \\
        --retire-named-after-property --yes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db.models import Hotel, HotelSource, PriceSeries, RoomType, Source  # noqa: E402
from app.db.session import sync_session  # noqa: E402
from app.services.rediscovery import names_echo_the_property  # noqa: E402
from app.workers.tasks_repair import purge_changes_for_rooms  # noqa: E402


def _orphan_changes(session, hotel_id: int):
    from app.db.models import PriceChange

    live = select(PriceSeries.offer_key).where(PriceSeries.hotel_id == hotel_id)
    return session.scalars(
        select(PriceChange).where(
            PriceChange.hotel_id == hotel_id,
            PriceChange.offer_key.not_in(live),
        )
    ).all()


def _count_orphan_changes(session, hotel_id: int) -> int:
    return len(_orphan_changes(session, hotel_id))


def _purge_orphan_changes(session, hotel_id: int) -> int:
    """Changes whose series no longer exists, and which therefore have no name.

    A price_change joins to its room through price_series on offer_key. When
    the series is gone -- retired with an invented room, or dropped because a
    repair changed the offer key -- the change survives with nothing to
    resolve, and the renderer prints "(room)".

    Scoped to one hotel because that is the blast radius of the action this
    script just took, and a global sweep is not a thing to do as a side effect
    of fixing one source.
    """
    doomed = _orphan_changes(session, hotel_id)
    for change in doomed:
        session.delete(change)
    return len(doomed)

#: Keys that describe the PAGE and are safe to copy between sources on the same
#: site. Everything else on a config belongs to the source that owns it -- the
#: repair budget, the discovery note, the URL fragment -- and copying those
#: would import another source's history along with its selectors.
_PAGE_KEYS = ("room_card", "wait_for", "selectors", "sold_out_markers", "wait_timeout_ms")


def _config_of(session, source_id: int) -> tuple[dict, str]:
    row = session.execute(
        select(HotelSource, Hotel.name)
        .join(Hotel, Hotel.id == HotelSource.hotel_id)
        .where(HotelSource.id == source_id)
    ).first()
    if row is None:
        raise SystemExit(f"No hotel_source with id={source_id}")
    return dict(row[0].adapter_config or {}), row[1]


def _hashed(config: dict) -> bool:
    """Does this config rest on generated class names?

    A crude test and an honest one: a class with no vowel-consonant rhythm and
    mixed case in the middle is a build artefact, not something a person wrote.
    Only used to WARN -- the operator decides.
    """
    blob = json.dumps({k: config.get(k) for k in ("room_card", "selectors")})
    return "text=" not in blob and any(
        c.isupper() for c in blob.replace("INR", "").replace("Rs", "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", type=int, required=True, help="hotel_source.id to fix.")
    parser.add_argument(
        "--like-source",
        type=int,
        required=True,
        help="hotel_source.id whose page config to copy. Must read its page correctly.",
    )
    parser.add_argument(
        "--retire-named-after-property",
        action="store_true",
        help="Also DELETE room types named after the hotel, and their price history.",
    )
    parser.add_argument("--yes", action="store_true", help="Actually write it.")
    args = parser.parse_args()

    with sync_session() as session:
        target = session.get(HotelSource, args.source_id)
        if target is None:
            raise SystemExit(f"No hotel_source with id={args.source_id}")
        hotel = session.get(Hotel, target.hotel_id)
        source = session.get(Source, target.source_id)

        donor_config, donor_hotel = _config_of(session, args.like_source)
        donor_source = session.execute(
            select(Source.adapter_key)
            .join(HotelSource, HotelSource.source_id == Source.id)
            .where(HotelSource.id == args.like_source)
        ).scalar_one()

        if donor_source != source.adapter_key:
            raise SystemExit(
                f"Refusing: donor runs {donor_source!r}, target runs "
                f"{source.adapter_key!r}. A page config is only portable "
                f"between sources of the same adapter."
            )

        current = dict(target.adapter_config or {})
        page = {k: donor_config[k] for k in _PAGE_KEYS if k in donor_config}

        print(f"TARGET  id={target.id}  {hotel.name}  [{source.adapter_key}]")
        for k in _PAGE_KEYS:
            if k not in current and k not in page:
                continue
            print(f"   {k}")
            print(f"      now  {current.get(k)!r}")
            if k in page:
                print(f"      new  {page[k]!r}")
            else:
                # The donor is silent on this key, so the target keeps what it
                # has. Replacing it with nothing would quietly drop a tuning
                # value -- a 45-second wait_timeout_ms, on a page that needs it.
                print("      new  (donor has none -- keeping the current value)")
        print(f"\nDONOR   id={args.like_source}  {donor_hotel}")
        if _hashed(page):
            print(
                "   ⚠  the donor's selectors look like generated class names.\n"
                "      They will break again when the site rebuilds its CSS.\n"
                "      Prefer a donor whose selectors are text= patterns."
            )
        else:
            print("   ✓  text-based selectors -- these survive a CSS rebuild.")

        doomed: list[RoomType] = []
        if args.retire_named_after_property:
            rooms = session.scalars(
                select(RoomType)
                .join(PriceSeries, PriceSeries.room_type_id == RoomType.id)
                .where(
                    PriceSeries.hotel_id == target.hotel_id,
                    PriceSeries.source_id == target.source_id,
                )
                .distinct()
            ).all()
            doomed = [r for r in rooms if names_echo_the_property(hotel.name, [r.name])]
            print("\nROOM TYPES TO DELETE (with all their price history):")
            for r in doomed:
                n = session.scalar(
                    select(PriceSeries)
                    .where(PriceSeries.room_type_id == r.id)
                    .with_only_columns(PriceSeries.offer_key)
                )
                print(f"   {r.name!r}   (series present: {'yes' if n else 'no'})")
            if not doomed:
                print("   (none -- no room type here is named after the hotel)")

        # Counted before the write so a deletion is never a surprise. These are
        # changes whose series is already gone -- they render as "(room)".
        orphans = _count_orphan_changes(session, target.hotel_id)
        if orphans:
            print(f"\nUNRESOLVABLE CHANGES TO DELETE: {orphans}")
            print("   Their price series no longer exists, so the renderer has")
            print("   no room name for them and prints the literal '(room)'.")

        if not args.yes:
            print("\nDRY RUN — nothing written. Re-run with --yes.")
            return 0

        # The repair state goes with it. A source that a person has just fixed
        # starts again with a full budget rather than inheriting the exhaustion
        # of the attempts that produced the config being replaced.
        # Only the keys the donor actually supplies are replaced. Rebuilding
        # from the donor alone drops every page key it happens not to carry,
        # and those are the tuning values -- a timeout the target needs and
        # the donor never did.
        restored = dict(current)
        restored.update(page)
        restored.pop("auto_repair", None)
        restored["discovery_note"] = (
            f"Restored from source {args.like_source} ({donor_hotel}) by "
            f"scripts/restore_source_config.py: discovery ranked this page "
            f"wrongly and declined its own repair, so automatic repair had no "
            f"move left."
        )
        target.adapter_config = restored

        purged = 0
        for room in doomed:
            # Before the delete: the cascade takes the series with the room,
            # and the series is the only thing joining a change to a name.
            purged += purge_changes_for_rooms(session, [room.id])
            session.delete(room)  # cascades to its price series and aliases

        # Changes left unresolvable by an EARLIER retirement -- including one
        # this script itself performed before it knew to do this.
        purged += _purge_orphan_changes(session, target.hotel_id)

        session.commit()
        print(
            f"\nWritten. {len(doomed)} room type(s) retired, "
            f"{purged} unresolvable change(s) removed."
        )
        print("Re-run scripts/inspect_source_config.py to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
