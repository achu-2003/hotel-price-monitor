"""Day-over-day price changes.

Adds ``price_changes.previous_offer_key``: set when a change was found by
comparing two different stay dates (tonight's opening price against last
night's closing price for the same room), NULL for the ordinary intraday
comparison where both prices share one ``offer_key``.

Nullable on purpose — every existing row predates the feature and is, by
definition, an intraday change, so NULL is the correct value for all of them
and no backfill is needed.

Revision ID: 0002_carry_over
Revises: 0001_initial
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_carry_over"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OFFER_KEY_LEN = 64  # sha256 hex digest


def upgrade() -> None:
    op.add_column(
        "price_changes",
        sa.Column("previous_offer_key", sa.String(length=OFFER_KEY_LEN), nullable=True),
    )
    # Partial: only carry-over rows have a value, and they are the minority.
    # Indexing the NULLs would be pure overhead on the hot intraday path.
    op.create_index(
        "ix_price_changes_previous_offer",
        "price_changes",
        ["previous_offer_key"],
        unique=False,
        postgresql_where=sa.text("previous_offer_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_price_changes_previous_offer", table_name="price_changes")
    op.drop_column("price_changes", "previous_offer_key")
