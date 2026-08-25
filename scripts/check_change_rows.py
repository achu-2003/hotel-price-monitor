"""Are the change rows we publish actually price changes?

Run it any time:

    .venv312\\Scripts\\python.exe scripts\\check_change_rows.py

WHAT IT ANSWERS
===============
A row on the Changes page is one of two things, and the COMPARED column says
which:

    same night     the same stay date, re-read later, and the price moved.
                   Always a real repricing.
    vs last night  the first time a stay date was ever priced, compared against
                   the night before it. There is nothing else to compare a
                   first sighting to, and without it a lead-0 target -- where
                   every morning is a brand-new stay date -- would never report
                   anything at all.

The second kind is only honest when both nights were watched the same way. A
night watched a week ahead and a night watched on the day are answers to
different questions, and the gap between them is not a repricing. This script
finds any row where that rule was broken.

It reads. It changes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db.session import sync_session  # noqa: E402

#: Every cross-night row, with the lead distance each side was watched at.
#: first_seen_at is the day the series was first priced, so the distance is
#: recovered from the data rather than from any target that may since have been
#: edited or deleted.
SQL = """
select
    to_char(pc.changed_at at time zone 'Asia/Kolkata', 'DD Mon HH24:MI') as when_,
    h.name                                                              as hotel,
    ps.check_in                                                         as night,
    prev.check_in                                                       as baseline,
    (ps.check_in   - (ps.first_seen_at   at time zone 'Asia/Kolkata')::date) as lead_now,
    (prev.check_in - (prev.first_seen_at at time zone 'Asia/Kolkata')::date) as lead_was,
    pc.old_price, pc.new_price
from price_changes pc
join price_series ps   on ps.offer_key   = pc.offer_key
join price_series prev on prev.offer_key = pc.previous_offer_key
join hotels h          on h.id           = pc.hotel_id
where pc.previous_offer_key is not null
  and pc.changed_at > now() - interval '30 days'
order by pc.changed_at desc
"""


def main() -> int:
    with sync_session() as session:
        rows = session.execute(text(SQL)).all()

    # A negative distance means the night had already passed when the series
    # was written -- scripts/backfill_carry_over.py, not a live reading. Those
    # are separated out because they are not the failure this checks for, and
    # counting them as one would make a clean run look dirty forever.
    backfilled = [r for r in rows if r.lead_now < 0 or r.lead_was < 0]
    mismatched = [
        r for r in rows if r not in backfilled and r.lead_now != r.lead_was
    ]

    print(f"cross-night rows in the last 30 days : {len(rows)}")
    print(f"  compared like for like             : {len(rows) - len(mismatched) - len(backfilled)}")
    print(f"  compared across different windows  : {len(mismatched)}")
    print(f"  involving a backfilled series      : {len(backfilled)}")

    if mismatched:
        print()
        print("These compared a night watched one way against a night watched")
        print("another. The difference between them is not a repricing:")
        print()
        for r in mismatched:
            print(f"  {r.when_}  {r.hotel[:24]:26} {r.night} (lead {r.lead_now})"
                  f"  vs {r.baseline} (lead {r.lead_was})"
                  f"   {r.old_price} -> {r.new_price}")
        print()
        print("Rows dated before the fix are expected and stay in history.")
        print("A row dated AFTER it means the rule is not holding — worth a look.")
    else:
        print()
        print("Every cross-night comparison was like for like.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
