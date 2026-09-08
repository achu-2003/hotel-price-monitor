"""A market summary on a clock, not an alert on every move.

Two columns, both defaulted so a deployment upgrading into this behaves
exactly as it did yesterday until somebody sets the interval.

``alert_defaults.summary_interval_hours``
    How often the market summary goes out: every N hours, one message per
    recipient saying how many rooms changed in that window, which rooms, and
    where the whole portfolio now sits against the market. ``0`` -- the
    default -- means it never goes out, which is what every deployment
    upgrading into this has asked for so far.

    Hours rather than minutes because the message is a reading of the market,
    and a market does not move usefully in less than an hour. An integer
    rather than a cron string because the one thing an operator wants to say
    here is "every two hours" and the one thing they must not be able to say
    by accident is "every two minutes".

``notifications.kind``
    What a message is about. Stored rather than worked out later, because a
    message held for quiet hours is rebuilt from its row hours after the
    window it covers has closed -- and it decides which approved WhatsApp
    template carries it. Backfilled to ``price_change``, which is what every
    row that exists when this runs actually is.

Both are ``ADD COLUMN ... DEFAULT``, which PostgreSQL 11 and later record in
the catalogue rather than rewriting the table, so this does not take a long
lock on a notifications table with a year of history in it.

Revision ID: 0013_market_comparison
Revises: 0012_price_with_tax
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_market_comparison"
down_revision = "0012_price_with_tax"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_defaults",
        sa.Column(
            "summary_interval_hours",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "notifications",
        sa.Column(
            "kind",
            sa.String(length=20),
            server_default="price_change",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("notifications", "kind")
    op.drop_column("alert_defaults", "summary_interval_hours")
