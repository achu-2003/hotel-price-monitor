"""Debounce state for a room that is absent rather than declared sold out

A partial page read is indistinguishable, at the moment it arrives, from a
hotel selling rooms. On 1 Sep a check returned 5 of one hotel's 9 rooms and 1
of another's 3 -- ``status=success``, ``sold_out=False``, no error anywhere --
and the four and two rooms that were merely missing from the HTML were
reported as sold out. Fourteen minutes later the next check got the full page
and reported all six as available again. Twelve alerts, no real events.

``missing_since`` gives the disappearance sweep the debounce the price
comparison has always had: a room absent from a page that still lists others
must be absent on two consecutive checks before anyone is told. A page that
positively declares itself sold out is still believed at once -- that is a
statement, not an absence.

Nullable with no default: every existing row starts at "not missing", which is
exactly right, and the first check after this migration establishes the truth.

Revision ID: 0010_missing_since
Revises: 0009_alert_numbers
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_missing_since"
down_revision = "0009_alert_numbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_series",
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_series", "missing_since")
