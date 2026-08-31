"""Recipients that follow every hotel, and that skip the throttle

Adds the two flags behind the WhatsApp alert numbers on the Alerts page: a
short list of phone numbers that get every price change, on every hotel,
straight away.

``alerts_all_hotels`` is coverage. The existing model needs one
hotel_recipients row per (hotel, person), which means a hotel added next month
reaches nobody until somebody remembers to assign it -- and that failure is
silent, because a hotel with no assignment looks exactly like a hotel with no
price movement. The flag is evaluated at dispatch instead, so new hotels are
covered the moment they exist.

``bypass_throttle`` is urgency: no quiet-hours hold, no per-recipient hourly
cap. Digest batching still applies, so one hotel's simultaneous changes remain
one message.

Both default to false, so every recipient that already exists keeps exactly
the behaviour it has now.

Revision ID: 0009_alert_numbers
Revises: 0008_check_run_sold_out
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_alert_numbers"
down_revision = "0008_check_run_sold_out"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipients",
        sa.Column(
            "alerts_all_hotels",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "recipients",
        sa.Column(
            "bypass_throttle",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("recipients", "bypass_throttle")
    op.drop_column("recipients", "alerts_all_hotels")
