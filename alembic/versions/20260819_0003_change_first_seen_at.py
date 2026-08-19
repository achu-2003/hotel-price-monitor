"""When a price change was first seen, not only when it was confirmed.

Adds ``price_changes.first_seen_at``.

A change is written on its SECOND consecutive sighting, so ``changed_at`` is
the moment the debounce completed. On a 30-minute target that is up to an hour
after the hotel actually moved its rate, and the dashboard was answering "when
did we finish checking" while appearing to answer "when did the price change".

Nullable, and not backfilled. Every existing row was written before the first
sighting was recorded, so the honest value for them is "unknown" — the
dashboard falls back to the confirmed time and says so, rather than presenting
a derived guess as an observation.

Revision ID: 0003_first_seen
Revises: 0002_carry_over
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_first_seen"
down_revision: str | None = "0002_carry_over"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_changes",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_changes", "first_seen_at")
