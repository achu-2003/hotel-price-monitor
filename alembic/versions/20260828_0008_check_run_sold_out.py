"""check_runs learns to say "sold out"

A run that read the page perfectly and found the hotel full looked, on every
screen, exactly like a run that found nothing and could not say why:

    success — 0 offers, 0 changes

Two columns close that gap.

``sold_out`` is the fact: the source announced no availability for the window
in ``check_in``/``check_out``. ``notes`` is the sentence a person reads, and
carries the other half of the new behaviour -- when a last-minute window is
full, the check rolls one night forward and prices THAT night instead, and the
row has to say which night it ended up reading (see
``app.services.dates.rollover_window``).

``notes`` is separate from ``error_summary`` on purpose: that column belongs to
runs that failed, and the dashboard renders it as a failure tooltip. A sold-out
run did not fail.

Revision ID: 0008_check_run_sold_out
Revises: 0007_username_login
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_check_run_sold_out"
down_revision = "0007_username_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "check_runs",
        sa.Column(
            "sold_out",
            sa.Boolean(),
            nullable=False,
            # Existing rows are backfilled false rather than guessed at. A run
            # from last week cannot be re-read, and "we do not know" is much
            # closer to false than to true here: the great majority of past
            # runs were ordinary successes.
            server_default=sa.text("false"),
        ),
    )
    op.add_column("check_runs", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("check_runs", "notes")
    op.drop_column("check_runs", "sold_out")
