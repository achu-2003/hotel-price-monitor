"""Attach a booking site to a hotel when discovery cannot read the page.

WHEN THIS IS THE RIGHT TOOL
===========================
Not often. Pasting the URL into "Add another site" is the way, because
discovery works out the engine, the selectors and the date placeholders and
records what it verified. This is for the case where it opens the page and
declines:

    Inspected the page but could not find prices it could trust. 13 JSON
    response(s) were left out of this reading because the site's robots.txt
    disallows them. The rendered page is the permitted surface here.

Declining is correct -- a configuration built on a disallowed path would read
that path on every check forever -- but it leaves the source unattached, and
on Booking.com that is the source the repricing rule needs: it prices against
the competitor's rate ON THE SITE OUR OWN PRICE COMES FROM, and Sterling's
own engine and their Booking.com listing differ by hundreds of rupees.

WHAT IT WRITES
==============
Two rows, because a site that is attached but not scheduled is never read and
looks broken rather than unconfigured:

  * ``hotel_sources``   the URL and the adapter config
  * ``monitor_targets`` how often to read it

NOT A HAND-AUTHORED SELECTOR. ``--like-hotel-source`` copies the config from a
source that is demonstrably reading its own page today, which keeps the thing
being written something the system produced and something you can point at.
``--config-file`` takes the same shape as JSON, for a machine with no such
source to copy from.

    python scripts/attach_source_with_config.py --hotel-id 10 \
        --source-code www-booking-com --url "https://www.booking.com/..." \
        --like-hotel-source 13

    python scripts/attach_source_with_config.py --hotel-id 10 \
        --source-code www-booking-com --url "https://www.booking.com/..." \
        --config-file booking-config.json --interval 30 --yes

It shows what it will do and needs ``--yes`` to do it. A hotel already
attached to that source is reported and left alone rather than duplicated.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Running `python scripts/attach_source_with_config.py` puts scripts/ on
# sys.path, not the project root, so `import app` would fail. Same fix as
# scripts/create_account.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import sync_session  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--hotel-id", type=int, required=True)
    ap.add_argument("--source-code", required=True,
                    help="the sources.code to attach, e.g. www-booking-com")
    ap.add_argument("--url", required=True,
                    help="with {check_in} and {check_out} where the dates go")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--like-hotel-source", type=int,
                     help="copy adapter_config from this hotel_sources.id")
    src.add_argument("--config-file", help="a JSON file holding adapter_config")
    ap.add_argument("--currency", default="INR")
    ap.add_argument("--interval", type=int, default=30,
                    help="minutes between checks (default 30)")
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--yes", action="store_true", help="actually write it")
    args = ap.parse_args()

    # A URL with real dates in it would pin every check to one night forever,
    # and the failure is silent: the same prices, every run, looking stale.
    if "{check_in}" not in args.url or "{check_out}" not in args.url:
        print("The URL needs {check_in} and {check_out} where the dates go.\n"
              "Paste the booking page's own URL and replace its two dates with "
              "those placeholders.", file=sys.stderr)
        return 2

    with sync_session() as s:
        hotel = s.execute(text("select id, name from hotels where id = :h"),
                          {"h": args.hotel_id}).mappings().first()
        if hotel is None:
            print(f"No hotel with id {args.hotel_id}.", file=sys.stderr)
            return 2

        source = s.execute(
            text("select id, code, display_name from sources where code = :c"),
            {"c": args.source_code}).mappings().first()
        if source is None:
            known = [r[0] for r in s.execute(text("select code from sources order by code"))]
            print(f"No source with code {args.source_code!r}. Known: {known}", file=sys.stderr)
            return 2

        existing = s.execute(
            text("select id from hotel_sources where hotel_id = :h and source_id = :s"),
            {"h": hotel["id"], "s": source["id"]}).scalar()
        if existing:
            print(f"{hotel['name']} is already attached to {source['code']} "
                  f"(hotel_sources.id {existing}). Nothing to do -- use the page's "
                  f"Replace box to change its URL.")
            return 0

        if args.like_hotel_source:
            row = s.execute(text(
                "select hs.adapter_config, h.name, so.code from hotel_sources hs "
                "join hotels h on h.id = hs.hotel_id "
                "join sources so on so.id = hs.source_id where hs.id = :i"),
                {"i": args.like_hotel_source}).mappings().first()
            if row is None:
                print(f"No hotel_sources row with id {args.like_hotel_source}.", file=sys.stderr)
                return 2
            config = dict(row["adapter_config"] or {})
            came_from = (f"{row['name']} / {row['code']} "
                         f"(hotel_sources.id {args.like_hotel_source})")
        else:
            with open(args.config_file, encoding="utf-8") as fh:
                config = dict(json.load(fh))
            came_from = args.config_file

        # The note is the only place a person reading this row later learns it
        # was not discovered. Discovery writes its own, claiming what it
        # verified; this replaces that rather than inheriting a claim about a
        # page it never looked at. The repair counters go too -- they belong to
        # the source they were earned on.
        config["discovery_note"] = (
            f"Attached by hand {datetime.now(UTC):%Y-%m-%d}: discovery could not read "
            f"the page, so the config was copied from {came_from}. NOT verified "
            f"against this URL -- check the first fetch."
        )
        config.pop("auto_repair", None)

        shown = {k: v for k, v in config.items() if k != "discovery_note"}
        print(f"  hotel     {hotel['name']} (id {hotel['id']})")
        print(f"  source    {source['display_name']} [{source['code']}] (id {source['id']})")
        print(f"  url       {args.url[:96]}{'...' if len(args.url) > 96 else ''}")
        print(f"  config    from {came_from}")
        print(f"            {json.dumps(shown)}")
        print(f"  schedule  every {args.interval} min, {args.adults} adults")
        if not args.yes:
            print("\nDry run. Re-run with --yes to write it.")
            return 0

        hotel_source_id = s.execute(text(
            "insert into hotel_sources "
            "  (hotel_id, source_id, url, currency, adapter_config, is_active, "
            "   created_at, updated_at) "
            "values (:h, :s, :u, :c, cast(:cfg as jsonb), true, now(), now()) "
            "returning id"),
            {"h": hotel["id"], "s": source["id"], "u": args.url,
             "c": args.currency, "cfg": json.dumps(config)}).scalar()

        s.execute(text(
            "insert into monitor_targets "
            "  (hotel_source_id, adults, children, rooms, date_strategy, "
            "   lead_time_days, length_of_stay_nights, interval_minutes, is_enabled, "
            "   next_run_at, confirm_checks, consecutive_failures, circuit_state, "
            "   created_at, updated_at) "
            "values (:hs, :a, 0, 1, 'rolling', 0, 1, :i, true, now(), 1, 0, 'closed', "
            "        now(), now())"),
            {"hs": hotel_source_id, "a": args.adults, "i": args.interval})
        s.commit()

        print(f"\nAttached. hotel_sources.id {hotel_source_id}, scheduled every "
              f"{args.interval} min.\nThe config is unverified against this page: "
              f"watch the hotel's next check before trusting its prices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
