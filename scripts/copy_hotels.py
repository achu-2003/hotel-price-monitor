#!/usr/bin/env python
"""Carry hotels from one deployment to another, by name.

WHY THIS EXISTS
===============
A hotel is set up where it is convenient -- usually on the laptop, where the
link can be pasted, the prices checked against the site, and a per-site choice
like "Use the price this link shows" made and verified. The server then needs
the same hotel. Pasting the link again there works, but it re-runs discovery
and drops every decision made since: a site whose settings were corrected, a
room renamed or retired, the link-price switch. This copies the result instead.

WHAT GOES ACROSS
================
Per hotel: the hotel row, its rooms, each booking site with its URL and its
adapter config (selectors, and choices like ``keep_link_deal``), the learned
room-name matches for that site, and each monitor target -- what is watched,
how often, with which alert sensitivity.

WHAT DOES NOT
=============
Prices and their history, check runs, alerts. Those are observations of a
past on the other machine; the server starts its own history at its first
check. Recipient assignments are not copied either: who is told about a hotel
is a decision about people on that deployment. Monitoring state (failures,
circuit) starts clean, and the first check is due immediately.

NAMES, NEVER IDS
================
Ids are per database -- hotel 13 here is not hotel 13 there -- so everything
is exported by name and resolved again on the way in: the booking site by its
source code, the room by its canonical name. A hotel that already exists on
the other side (same name, same owner) is REPORTED AND SKIPPED, never merged:
two setups of one hotel silently blended is worse than one that is plainly
not copied. A booking site whose source does not exist there is skipped the
same way.

USE
===
On the machine that has the hotels::

    .venv312\\Scripts\\python.exe scripts/copy_hotels.py --export hotels.json \\
        --hotels "Peters Park, MSB Silver Spring Resort, Hotel Hills, Hotel Kumararraja Palace"

Copy the file across, then on the server::

    .venv312\\Scripts\\python.exe scripts/copy_hotels.py --import hotels.json
    .venv312\\Scripts\\python.exe scripts/copy_hotels.py --import hotels.json --yes

It shows what it would create and needs ``--yes`` to write. Importing twice is
safe: the second run finds the hotels there and skips them.

Pass ``--owner`` when the deployment has more than one account. Hotel names
are matched ignoring case and stray spaces, so " Hotel Kumararraja Palace"
is found by "Hotel Kumararraja Palace".
"""
from __future__ import annotations

import argparse
import enum
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

# Running `python scripts/copy_hotels.py` puts scripts/ on sys.path, not the
# project root, so `import app` would fail. Same fix as copy_room_mappings.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, select  # noqa: E402

from app.db.models import (  # noqa: E402
    Hotel, HotelSource, MonitorTarget, RoomType, RoomTypeAlias, Source, User,
)
from app.db.session import sync_session  # noqa: E402

#: Columns never copied: ids and foreign keys are per database, and the rest is
#: history or state that belongs to the machine that recorded it.
_NOT_COPIED = {
    "id", "hotel_id", "source_id", "hotel_source_id", "room_type_id", "owner_user_id",
    "created_at", "updated_at", "last_verified_at",
    "next_run_at", "last_success_at", "last_failure_at",
    "robots_checked_at", "robots_allows",
    "consecutive_failures", "circuit_state", "circuit_opened_at",
}


def _key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def _out(row) -> dict:
    """A row's copyable columns as JSON-safe values."""
    data = {}
    for column in inspect(type(row)).columns:
        if column.key in _NOT_COPIED:
            continue
        value = getattr(row, column.key)
        if isinstance(value, enum.Enum):
            value = value.value
        elif isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()
        data[column.key] = value
    return data


def _in(model, data: dict) -> dict:
    """``_out`` reversed: JSON values back to what each column takes."""
    columns = {c.key: c for c in inspect(model).columns}
    values = {}
    for key, value in data.items():
        column = columns.get(key)
        if column is None or key in _NOT_COPIED:
            continue
        if value is None:
            values[key] = None
            continue
        enum_class = getattr(column.type, "enum_class", None)
        python_type = None
        try:
            python_type = column.type.python_type
        except NotImplementedError:
            pass
        if enum_class is not None:
            value = enum_class(value)
        elif python_type is Decimal:
            value = Decimal(value)
        elif python_type is date:
            value = date.fromisoformat(value)
        elif python_type is datetime:
            value = datetime.fromisoformat(value)
        values[key] = value
    return values


def _owner(session, username: str | None) -> User:
    """The account the hotels belong to. Named when there is more than one."""
    if username:
        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            raise SystemExit(f"No user {username!r} here.")
        return user
    users = session.scalars(select(User).order_by(User.id)).all()
    owners = {h.owner_user_id for h in session.scalars(select(Hotel))} - {None}
    candidates = [u for u in users if u.id in owners] or users
    if len(candidates) == 1:
        return candidates[0]
    raise SystemExit("More than one account owns hotels here; say which with --owner. Known: "
                     + ", ".join(u.username for u in candidates))


def export(path: Path, wanted: list[str], username: str | None) -> int:
    with sync_session() as session:
        owner = _owner(session, username)
        hotels = session.scalars(select(Hotel).where(Hotel.owner_user_id == owner.id)).all()
        by_key = {_key(h.name): h for h in hotels}
        missing = [w for w in wanted if _key(w) not in by_key]
        if missing:
            raise SystemExit("Not found for " + owner.username + ": " + ", ".join(missing)
                             + "\nKnown: " + ", ".join(sorted(h.name.strip() for h in hotels)))

        out = []
        for name in wanted:
            hotel = by_key[_key(name)]
            rooms = session.scalars(select(RoomType).where(RoomType.hotel_id == hotel.id)
                                    .order_by(RoomType.sort_order, RoomType.id)).all()
            canonical = {r.id: r.canonical_name for r in rooms}
            sites = []
            for hs, source in session.execute(
                select(HotelSource, Source).join(Source, HotelSource.source_id == Source.id)
                .where(HotelSource.hotel_id == hotel.id).order_by(HotelSource.id)
            ):
                aliases = session.scalars(select(RoomTypeAlias).where(
                    RoomTypeAlias.hotel_id == hotel.id, RoomTypeAlias.source_id == source.id)).all()
                targets = session.scalars(select(MonitorTarget).where(
                    MonitorTarget.hotel_source_id == hs.id).order_by(MonitorTarget.id)).all()
                sites.append({
                    "source_code": source.code, **_out(hs),
                    "aliases": [{"room": canonical.get(a.room_type_id), **_out(a)} for a in aliases
                                if a.room_type_id in canonical],
                    "targets": [_out(t) for t in targets],
                })
            out.append({**_out(hotel), "rooms": [_out(r) for r in rooms], "sites": sites})
            print(f"  {hotel.name.strip()}: {len(rooms)} rooms, "
                  + ", ".join(f"{s['source_code']} ({len(s['targets'])} watched)" for s in sites))

        # The booking sites these hotels are read from, so a deployment that has
        # never used one can be given it -- with its Terms review -- rather than
        # receiving hotels it has no way to check.
        codes = {site["source_code"] for hotel in out for site in hotel["sites"]}
        exported_sources = {
            src.code: {k: v for k, v in _out(src).items() if k != "code"}
            for src in session.scalars(select(Source).where(Source.code.in_(codes)))
        }

    path.write_text(json.dumps({"owner": owner.username, "sources": exported_sources, "hotels": out},
                               indent=2, ensure_ascii=False),
                    encoding="utf-8")
    print(f"Wrote {len(out)} hotel(s) to {path}.")
    return 0


def _source_for(session, code: str, exported: dict | None, sources: dict, notes: list) -> Source | None:
    """The booking site ``code`` here, created from the export if it is missing.

    Created WITH the Terms review recorded on the machine it came from: that is
    a decision somebody already made and put their name to, and without it
    nothing on the site is fetched. Printed, so it is not made silently twice.
    """
    if code in sources:
        return sources[code]
    if not exported:
        return None
    source = Source(**_in(Source, exported), code=code)
    session.add(source)
    session.flush()
    sources[code] = source
    reviewed = (f"Terms reviewed by {source.tos_reviewed_by} on {source.tos_reviewed_at}"
                if source.tos_reviewed_at else "NO Terms review recorded: nothing is fetched until one is")
    notes.append(f"  + booking site {code} ({source.display_name}) -- {reviewed}")
    return source


def import_(path: Path, username: str | None, write: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    exported_sources = data.get("sources", {})
    with sync_session() as session:
        owner = _owner(session, username)
        existing = {_key(h.name): h for h in session.scalars(
            select(Hotel).where(Hotel.owner_user_id == owner.id))}
        slugs = set(session.scalars(select(Hotel.slug)))
        sources = {s.code: s for s in session.scalars(select(Source))}
        now = datetime.now(UTC)
        changed = 0

        for item in data["hotels"]:
            name = item["name"].strip()
            notes: list[str] = []
            # Resolve every site first: a hotel with nothing to read is a row
            # that looks set up and never gets a price.
            usable = []
            for site in item["sites"]:
                source = _source_for(session, site["source_code"],
                                     exported_sources.get(site["source_code"]), sources, notes)
                if source is None:
                    notes.append(f"    - {site['source_code']}: SKIPPED, no such booking site here "
                                 f"and none in the file")
                else:
                    usable.append((site, source))

            hotel = existing.get(_key(name))
            if hotel is not None:
                # ALREADY HERE: only what it is missing is added. A site it
                # already has is left exactly as it is -- never merged.
                have = set(session.scalars(select(HotelSource.source_id)
                                           .where(HotelSource.hotel_id == hotel.id)))
                usable = [(site, source) for site, source in usable if source.id not in have]
                if not usable:
                    print(f"SKIP {name}: already here with all its booking sites.")
                    continue
                verb = "ADD TO" if write else "WOULD ADD TO"
            else:
                if not usable:
                    print(f"SKIP {name}: none of its booking sites exist here, so it would never "
                          f"be checked.")
                    print("\n".join(notes))
                    continue
                hotel_values = _in(Hotel, {k: v for k, v in item.items() if k not in ("rooms", "sites")})
                hotel_values["name"] = name
                if hotel_values["slug"] in slugs:
                    print(f"SKIP {name}: another hotel here already uses the slug "
                          f"{hotel_values['slug']!r}.")
                    continue
                hotel = Hotel(**hotel_values, owner_user_id=owner.id)
                session.add(hotel)
                session.flush()
                slugs.add(hotel.slug)
                existing[_key(name)] = hotel
                verb = "CREATE" if write else "WOULD CREATE"

            rooms = {r.canonical_name: r for r in session.scalars(
                select(RoomType).where(RoomType.hotel_id == hotel.id))}
            for room in item["rooms"]:
                if room["canonical_name"] not in rooms:
                    row = RoomType(**_in(RoomType, room), hotel_id=hotel.id)
                    session.add(row)
                    rooms[room["canonical_name"]] = row
            session.flush()

            for site, source in usable:
                values = _in(HotelSource, {k: v for k, v in site.items()
                                           if k not in ("source_code", "aliases", "targets")})
                hs = HotelSource(**values, hotel_id=hotel.id, source_id=source.id)
                session.add(hs)
                session.flush()
                for alias in site["aliases"]:
                    room = rooms.get(alias["room"])
                    if room is not None:
                        session.add(RoomTypeAlias(
                            **_in(RoomTypeAlias, {k: v for k, v in alias.items() if k != "room"}),
                            room_type_id=room.id, hotel_id=hotel.id, source_id=source.id))
                for target in site["targets"]:
                    # Due now, clean state: this machine has never checked it.
                    session.add(MonitorTarget(**_in(MonitorTarget, target),
                                              hotel_source_id=hs.id, next_run_at=now))
                flags = " + uses the price its link shows" if (hs.adapter_config or {}).get("keep_link_deal") else ""
                notes.append(f"    - {source.code}: {len(site['targets'])} watched, "
                             f"{len(site['aliases'])} room matches{flags}")
            session.flush()
            print(f"{verb} {name}: {len(rooms)} rooms")
            print("\n".join(notes))
            changed += 1

        if write:
            session.commit()
            print(f"Done: {changed} hotel(s) created or completed. Their first checks are due now.")
        else:
            session.rollback()
            print(f"Nothing written. {changed} hotel(s) would be created or completed; "
                  f"run again with --yes.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export", type=Path, metavar="FILE")
    mode.add_argument("--import", dest="import_", type=Path, metavar="FILE")
    parser.add_argument("--hotels", help="comma-separated hotel names to export")
    parser.add_argument("--owner", help="the account the hotels belong to")
    parser.add_argument("--yes", action="store_true", help="write the import")
    args = parser.parse_args()

    if args.export:
        wanted = [n.strip() for n in (args.hotels or "").split(",") if n.strip()]
        if not wanted:
            parser.error("--export needs --hotels")
        return export(args.export, wanted, args.owner)
    return import_(args.import_, args.owner, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
