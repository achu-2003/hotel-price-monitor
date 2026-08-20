"""Why a change told nobody, and who hears about the monitor itself.

Adds ``price_changes.suppressed_reason`` and ``recipients.receives_ops_alerts``.

WHY ``suppressed_reason``
=========================
``dispatch_changes`` marks a change ``notified=True`` even when it found nobody
to tell -- it has to, or the change reappears in every dispatch sweep forever.
The cost was that the flag conflated three different outcomes:

    sent to somebody | nobody was assigned | everybody's threshold filtered it

Only the first is success and all three looked identical afterwards, so a
misconfigured deployment reported the same state as a working one. This column
records which of the three happened, which makes "12 changes last week reached
nobody" a query instead of a guess -- and makes a future "send it anyway"
button possible, since the changes that went nowhere can now be found.

NULL means "sent, or not dispatched yet"; the ``notified`` flag distinguishes
those two and keeps its old meaning untouched.

WHY ``receives_ops_alerts``
===========================
``alert_on_silence`` detects the failure that actually costs money -- a target
that stopped succeeding without ever erroring -- and, until now, wrote a log
line about it. Nobody reads log lines. This flags the people who should be told
when the monitoring itself goes quiet, which is a different question from which
hotels they follow: the person who wants to know the scraper broke is usually
not the person who wants every rate move on one property.

Defaults to false. Turning it on is a decision, and a system that silently
enrolled everyone in ops noise would get its alerts muted within a week.

Revision ID: 0004_suppression_ops
Revises: 0003_first_seen
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_suppression_ops"
down_revision: str | None = "0003_first_seen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_changes",
        sa.Column("suppressed_reason", sa.String(length=32), nullable=True),
    )
    # Partial index: the interesting rows are the suppressed minority, and
    # indexing the NULLs would be most of the table for no benefit.
    op.create_index(
        "ix_price_changes_suppressed",
        "price_changes",
        ["suppressed_reason", "changed_at"],
        postgresql_where=sa.text("suppressed_reason IS NOT NULL"),
    )

    op.add_column(
        "recipients",
        sa.Column(
            "receives_ops_alerts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("recipients", "receives_ops_alerts")
    op.drop_index("ix_price_changes_suppressed", table_name="price_changes")
    op.drop_column("price_changes", "suppressed_reason")
