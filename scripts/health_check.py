#!/usr/bin/env python
"""Is production actually working? One verdict per hotel, from the data itself.

    .venv312/Scripts/python.exe scripts/health_check.py

Read-only. Safe on a serving box.

WHY ERROR COUNTS DO NOT ANSWER THIS
===================================
An empty Attention screen means nothing failed loudly. It does not mean the
numbers on the dashboard are right, and the failures that cost money have
always been the quiet ones:

  * a hotel monitored for two days against the similar-hotels carousel,
    reporting four neighbouring properties as its own rooms
  * a six-room property tracked as ONE room for weeks, because the name
    selector found an amenity chip reading "King Size Bed" on every card
  * a series that stopped moving while checks kept succeeding

Every one of those is a green screen. So this asks the opposite question --
not "did anything raise an error" but "does what we collected look like a
hotel we are really watching".

WHAT EACH VERDICT MEANS
=======================
    LATE      no successful check within twice its own interval
    BREAKER   the circuit is open; nothing is being fetched at all
    COLLAPSED less than half the rooms this hotel showed over the past fortnight
    FROZEN    prices have not moved in days while checks kept succeeding
    NAMES     a room name that does not read like a room name
    OK        none of the above
"""
from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import sync_session  # noqa: E402

# A room name is a PRODUCT. These are the shapes that turned out not to be
# one, each taken from a config that was silently wrong in production.
_AMENITY_WORDS = {
    "bed", "beds", "king size bed", "queen size bed", "double bed", "twin bed",
    "wifi", "free wifi", "breakfast", "room with breakfast", "air conditioning",
    "ac", "tv", "pool", "parking", "non smoking", "smoking",
}
_FRAGMENT = re.compile(r"^(bed|bedroom|beds|room|rooms)\s*[:\-]?\s*$", re.I)

FROZEN_DAYS = 5


def suspect(name: str) -> str | None:
    """Why this name is not a room name, or None if it reads like one."""
    n = " ".join((name or "").lower().split())
    if not n:
        return "empty"
    if _FRAGMENT.match(n):
        return "a fragment, not a name"
    if n in _AMENITY_WORDS:
        return "an amenity, not a room"
    if len(n) < 4:
        return "too short to be a room name"
    if n.endswith(":"):
        return "ends in a colon -- a label, not a name"
    return None


def main() -> int:
    now = datetime.now(UTC)
    worst = 0

    with sync_session() as session:
        targets = session.execute(text("""
            select h.id, h.name, s.code, mt.interval_minutes, mt.last_success_at,
                   mt.consecutive_failures, mt.circuit_state::text
            from monitor_targets mt
            join hotel_sources hs on hs.id = mt.hotel_source_id
            join hotels h on h.id = hs.hotel_id
            join sources s on s.id = hs.source_id
            where mt.is_enabled and h.is_active
            order by h.name
        """)).all()

        rooms = {r[0]: (r[1], r[2]) for r in session.execute(text("""
            select ps.hotel_id,
                   count(distinct ps.room_type_id)
                       filter (where ps.last_checked_at > now() - interval '24 hours'),
                   count(distinct ps.room_type_id)
            from price_series ps
            where ps.last_checked_at > now() - interval '14 days'
            group by 1
        """)).all()}

        moved = {r[0]: r[1] for r in session.execute(text("""
            select hotel_id, max(last_changed_at) from price_series group by 1
        """)).all()}

        readings = {r[0]: r[1] for r in session.execute(text("""
            select ps.hotel_id, count(*)
            from price_observations o
            join price_series ps on ps.offer_key = o.offer_key
            where o.checked_at > now() - interval '24 hours'
            group by 1
        """)).all()}

        names: dict[int, list[tuple[str, str]]] = {}
        for hotel_id, name in session.execute(text("""
            select hotel_id, name from room_types where is_active
        """)).all():
            why = suspect(name)
            if why:
                names.setdefault(hotel_id, []).append((name, why))

        print(f"  {'hotel':<34} {'source':<18} {'rooms':>7} {'reads':>6}  verdict")
        print("  " + "-" * 86)

        for hid, hname, code, interval, last_ok, fails, circuit in targets:
            flags, detail = [], []

            if circuit != "closed":
                flags.append("BREAKER")
                detail.append(f"circuit is {circuit} -- nothing is being fetched")
            age_min = (now - last_ok).total_seconds() / 60 if last_ok else None
            if age_min is None:
                flags.append("LATE")
                detail.append("has never succeeded")
            elif age_min > 2 * interval:
                flags.append("LATE")
                detail.append(
                    f"last success {age_min/60:.1f}h ago, interval is {interval} min")

            # MOST of the list gone, not merely some of it. A hotel lists
            # different rooms on different nights and stops listing the ones
            # it has sold, so "fewer than the fortnight's total" is the normal
            # state of every healthy row here -- it flagged 4 of 10 on the
            # first run, which is how a check teaches people to ignore it.
            # Half is the line: a name selector that has landed on a shared
            # label takes nearly the whole list with it.
            today, fortnight = rooms.get(hid, (0, 0))
            if fortnight and today * 2 < fortnight:
                flags.append("COLLAPSED")
                detail.append(
                    f"only {today} room(s) today against {fortnight} over the "
                    f"past fortnight")

            last_move = moved.get(hid)
            if last_move and age_min is not None:
                days = (now - last_move).days
                if days >= FROZEN_DAYS:
                    flags.append("FROZEN")
                    detail.append(f"no price has moved in {days} days")

            if hid in names:
                flags.append("NAMES")
                for n, why in names[hid][:3]:
                    detail.append(f"room named {n!r} -- {why}")

            if fails:
                detail.append(f"{fails} consecutive failure(s)")

            verdict = " ".join(flags) if flags else "OK"
            worst = max(worst, 1 if flags else 0)
            print(f"  {hname[:34]:<34} {code[:18]:<18} "
                  f"{today:>7} {readings.get(hid, 0):>6}  {verdict}")
            for line in detail:
                print(f"  {'':<34} {'':<18} {'':>7} {'':>6}    - {line}")

        # Is the SCHEDULER keeping up? Every target above can read OK while
        # beat has stopped and the whole board is quietly hours old.
        expected = sum(60 / t[3] for t in targets)
        actual = session.scalar(text(
            "select count(*) from check_runs where started_at > now() - interval '1 hour'"
        )) or 0
        print()
        print(f"  checks in the last hour : {actual}, expected about {expected:.0f}")
        if actual < expected * 0.6:
            print("  >> THE SCHEDULER IS BEHIND. Check beat and the workers.")
            worst = 1

    print()
    if worst:
        print("  Something above needs a look. None of it is read off the error")
        print("  screen -- every line is what the collected data itself says.")
    else:
        print("  Every hotel current, every room list intact, the scheduler on rate.")
        print("  The numbers behind the dashboard are the hotels you are watching.")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
