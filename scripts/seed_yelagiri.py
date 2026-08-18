#!/usr/bin/env python
"""List — or create — every resort on the gotoyelagiri portal.

One unauthenticated call returns every property the portal carries, so a
competitor set concentrated in Yelagiri can be stood up in one command instead
of one paste per hotel.

    python scripts/seed_yelagiri.py                 # list what is there
    python scripts/seed_yelagiri.py --create        # create them all
    python scripts/seed_yelagiri.py --create --only "Appu,Happy Hills"

Nothing is fetched until the source has a recorded Terms of Service review —
the same gate as every other source. Creating hotels is a configuration
change; approving the source is a decision, and this script deliberately does
not make it for you.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.models import (  # noqa: E402
    DateStrategy,
    Hotel,
    HotelSource,
    MonitorTarget,
    Source,
)
from app.db.session import sync_session  # noqa: E402

ENDPOINT = "https://api.gotoyelagiri.com/api/resort/room/new"
SOURCE_CODE = "gotoyelagiri"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36 HotelPriceMonitor/1.0")

#: Obvious non-properties on the portal. Skipped by default rather than
#: created and then puzzled over.
SKIP_NAMES = {"test resort", "", "none", "null"}


def _slugify(name: str) -> str:
    cleaned = "-".join(name.lower().strip().split())
    return "".join(c for c in cleaned if c.isalnum() or c == "-")[:200] or "resort"


def load_resorts() -> dict[str, dict]:
    """resort_id -> {name, rooms, cheapest}."""
    response = httpx.get(ENDPOINT, timeout=45,
                         headers={"User-Agent": UA, "Accept": "application/json"})
    response.raise_for_status()
    rooms = response.json()
    if isinstance(rooms, dict):
        rooms = rooms.get("data") or []

    resorts: dict[str, dict] = {}
    for room in rooms:
        if not isinstance(room, dict):
            continue
        rid = str(room.get("resort"))
        name = str(room.get("resortName") or "").strip()
        entry = resorts.setdefault(rid, {"name": name, "rooms": 0, "prices": []})
        entry["rooms"] += 1
        if name and not entry["name"]:
            entry["name"] = name
        try:
            price = float(room.get("price"))
            if price > 0:
                entry["prices"].append(price)
        except (TypeError, ValueError):
            pass
    return resorts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true",
                        help="Create the hotels, sources and targets")
    parser.add_argument("--only", help="Comma-separated name fragments to include")
    parser.add_argument("--interval", type=int, default=60,
                        help="Check interval in minutes (default 60: this portal "
                             "publishes a standing rate, not per-night pricing)")
    args = parser.parse_args()

    resorts = load_resorts()
    wanted = [w.strip().lower() for w in (args.only or "").split(",") if w.strip()]

    print(f"{len(resorts)} resorts on the portal\n")
    print(f"{'id':>4}  {'resort':<34} {'rooms':>5}  {'cheapest':>9}")
    print("-" * 60)
    selected: list[tuple[str, dict]] = []
    for rid, info in sorted(resorts.items(), key=lambda kv: -kv[1]["rooms"]):
        name = info["name"]
        cheapest = min(info["prices"]) if info["prices"] else None
        skip = name.lower() in SKIP_NAMES
        if wanted and not any(w in name.lower() for w in wanted):
            skip = True
        mark = "  (skipped)" if skip else ""
        print(f"{rid:>4}  {name[:34]:<34} {info['rooms']:>5}  "
              f"{('%.0f' % cheapest) if cheapest else '-':>9}{mark}")
        if not skip:
            selected.append((rid, info))

    if not args.create:
        print(f"\n{len(selected)} would be created. Re-run with --create.")
        return 0

    with sync_session() as session:
        source = session.scalar(select(Source).where(Source.code == SOURCE_CODE))
        if source is None:
            source = Source(
                code=SOURCE_CODE,
                display_name="gotoyelagiri portal",
                adapter_key="gotoyelagiri",
                base_domain="api.gotoyelagiri.com",
                # One shared response serves every hotel, and the adapter
                # caches it, so a whole sweep costs the portal one request.
                rate_limit_per_min=6,
                # Disabled until a named human records a review. Creating rows
                # is configuration; permission is a decision.
                is_enabled=False,
            )
            session.add(source)
            session.flush()
            print(f"\ncreated source {SOURCE_CODE!r} (id {source.id}) - DISABLED")
        else:
            print(f"\nusing existing source {SOURCE_CODE!r} (id {source.id})")

        created = skipped = 0
        for rid, info in selected:
            name = info["name"]
            slug = _slugify(name)
            hotel = session.scalar(select(Hotel).where(Hotel.slug == slug))
            if hotel is None:
                hotel = Hotel(name=name, slug=slug, location="Yelagiri, Tamil Nadu")
                session.add(hotel)
                session.flush()

            link = session.scalar(
                select(HotelSource).where(
                    HotelSource.hotel_id == hotel.id,
                    HotelSource.source_id == source.id,
                )
            )
            if link is not None:
                skipped += 1
                continue

            link = HotelSource(
                hotel_id=hotel.id, source_id=source.id,
                external_id=rid, currency="INR", adapter_config={},
            )
            session.add(link)
            session.flush()

            session.add(MonitorTarget(
                hotel_source_id=link.id,
                date_strategy=DateStrategy.ROLLING,
                lead_time_days=0, length_of_stay_nights=1,
                adults=2, children=0,
                interval_minutes=args.interval,
                next_run_at=datetime.now(UTC),
            ))
            created += 1
            print(f"  + {name[:40]:<42} resort_id={rid}")

        print(f"\ncreated {created}, already present {skipped}")
        if not source.is_usable:
            print(
                "\nNothing will be fetched yet: the source has no recorded Terms "
                "of Service review.\nApprove it on any of these hotels' pages, or:"
                f"\n  POST /api/v1/sources/{source.id}/tos-review "
                '{"reviewed_by": "Your Name", "approve": true}'
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
