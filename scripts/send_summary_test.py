"""Send ONE real market-summary message, on demand, to one recipient.

WHY THIS EXISTS
===============
The summary arrives when a window closes. With a two-hour interval that is a
wait of up to two hours, and with the interval at 0 it never arrives at all —
which is the right behaviour in production and useless when the question is
"does the approved template render properly on my phone".

``POST /api/v1/notifications/test`` does not answer it either: that endpoint
sends ``render_digest``, the price-change shape, through the price-change
template. It proves the CHANNEL works and says nothing about the summary
template, its parameter count, or how a seven-hundred-character rate table
looks once WhatsApp has wrapped it.

So this sends the real thing: the same renderer the worker uses, the same
provider, to a real number. The moves are taken from the last ``--hours`` of
real price changes, so every variable carries text the template will actually
receive rather than a placeholder that renders differently.

    python scripts/send_comparison_test.py                     # show me
    python scripts/send_comparison_test.py --recipient 1 --yes # send it
    python scripts/send_comparison_test.py --recipient 1 --hours 24 --yes

IT COSTS A MESSAGE, so it will not send without ``--yes``. With no flags it
prints the exact parameters that would go out and stops. Read them first: a
parameter count that disagrees with what Meta approved is refused by the
provider before it spends anything, but a template whose ORDER disagrees sends
happily and puts the night where the property name should be.

``--template`` overrides ``WHATSAPP_COMPARISON_TEMPLATE_NAME`` for this send
alone, so a freshly approved name can be tried before it is written into
``.env`` — the one edit you do not want to make twice on a production box.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The whole point of this script is to SHOW you the parameters, and every one
# of them carries a rupee sign, an arrow or a triangle. A Windows console is
# cp1252, so printing them raises UnicodeEncodeError and the script dies
# halfway through the thing it exists to print. Reconfigured rather than
# stripped: the characters are what actually goes to WhatsApp, and a preview
# that quietly removed them would be a preview of a different message.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Hotel, PriceChange, Recipient  # noqa: E402
from app.db.session import sync_session  # noqa: E402
from app.notifications import registry  # noqa: E402
from app.notifications.base import Destination  # noqa: E402
from app.notifications.render import render_summary  # noqa: E402
from app.services import monitoring as monitoring_service  # noqa: E402
from app.workers.tasks_notify import _render_lines  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", type=int, help="Recipient id to send to.")
    parser.add_argument("--channel", default="whatsapp", help="whatsapp or email.")
    parser.add_argument(
        "--template",
        help="Override the comparison template name for this send only.",
    )
    parser.add_argument(
        "--params",
        type=int,
        help="Override how many body variables the template was approved with.",
    )
    parser.add_argument(
        "--shorten",
        type=int,
        metavar="N",
        help=(
            "Truncate every parameter to N characters. Diagnostic only: it "
            "proves whether a failure is about the SIZE of the request rather "
            "than the credentials or the template, because everything else "
            "about the send is identical."
        ),
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=2,
        help=(
            "How far back to look for the moved rooms that fill {{1}} and "
            "{{2}}, and the window the message names. Default 2."
        ),
    )
    parser.add_argument("--yes", action="store_true", help="Actually send it.")
    args = parser.parse_args()

    settings = get_settings()

    with sync_session() as session:
        recipients = session.scalars(
            select(Recipient).where(Recipient.is_active.is_(True)).order_by(Recipient.id)
        ).all()
        if args.recipient is None:
            print("Active recipients:\n")
            for r in recipients:
                print(f"  --recipient {r.id:<3} {r.name!r}  {r.phone_e164 or '(no number)'}")
            print("\nPick one and re-run with --recipient <id>.")
            return 0

        recipient = next((r for r in recipients if r.id == args.recipient), None)
        if recipient is None:
            print(f"No active recipient with id {args.recipient}.")
            return 1

        # The real moves from the last --hours, so every variable carries the
        # text that will actually go out. An empty window is not an error here:
        # a template renders differently with one room in it than with twenty,
        # and seeing the empty case is worth a run of its own.
        since = datetime.now(UTC) - timedelta(hours=args.hours)
        changes = session.scalars(
            select(PriceChange).where(PriceChange.changed_at >= since)
        ).all()
        hotels = {
            h.id: h
            for h in session.scalars(
                select(Hotel).where(Hotel.id.in_({c.hotel_id for c in changes}))
            )
        }
        # The same switch the dispatcher reads, so this rehearsal quotes the
        # numbers a real summary would.
        with_tax = monitoring_service.alert_prices_with_tax()
        lines = _render_lines(session, list(changes), hotels, with_tax)
        moved = [lines[c.id] for c in sorted(changes, key=lambda c: c.id) if c.id in lines]

        template = args.template or settings.whatsapp_comparison_template_name
        count = args.params or settings.whatsapp_comparison_template_params

        message = render_summary(
            moved,
            window_hours=args.hours,
            when=datetime.now(UTC),
            param_count=count,
            with_tax=with_tax,
        )

        if args.shorten:
            # Same message, same template, same credentials -- less text.
            short = [
                (p if len(p) <= args.shorten else p[: args.shorten - 1].rstrip() + "…")
                for p in (message.template_params or [])
            ]
            message = replace(message, template_params=short)

        print(f"To:        {recipient.name!r} {recipient.phone_e164 or recipient.email}")
        print(f"Channel:   {args.channel}")
        rooms = len({(" ".join(m.hotel_name.split()), m.room_name) for m in moved})
        print(f"Moved:     {rooms} rooms, {len(moved)} price changes, "
              f"last {args.hours}h")
        print(f"Template:  {template or '(NOT CONFIGURED)'}  expecting {count} variables")
        print(f"Built:     {len(message.template_params or [])} variables\n")
        for index, value in enumerate(message.template_params or [], 1):
            print(f"  {{{{{index}}}}}  {value}\n")

        if not template:
            print(
                "No market-summary template is configured. Set "
                "WHATSAPP_COMPARISON_TEMPLATE_NAME, or pass --template, or the "
                "provider will refuse this send."
            )
            if args.channel == "whatsapp":
                return 1

        if not args.yes:
            print("DRY RUN — nothing was sent. Re-run with --yes to send it for real.")
            return 0

        provider = registry.get_provider(args.channel)
        if not provider.is_configured():
            print(f"The {args.channel} provider ({provider.provider_name}) is not configured.")
            return 1

        # The override has to reach the provider, which reads the name off
        # settings rather than off the message. Patched on the cached settings
        # object for the life of this process only; nothing is written to .env.
        if args.template:
            settings.whatsapp_comparison_template_name = args.template
        if args.params:
            settings.whatsapp_comparison_template_params = args.params

        result = provider.send(
            Destination(
                name=recipient.name,
                email=recipient.email,
                phone_e164=recipient.phone_e164,
            ),
            message,
        )

        if result.ok:
            print(f"SENT via {provider.provider_name}. Provider id: {result.provider_message_id}")
            return 0
        print(f"FAILED via {provider.provider_name}: {result.error_code} — {result.error_detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
