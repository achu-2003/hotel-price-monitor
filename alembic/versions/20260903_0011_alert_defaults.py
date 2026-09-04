"""Alert sensitivity, editable without a deploy

How big a price move has to be before anybody is told lived in Settings:
DEFAULT_MIN_DELTA_ABS, DEFAULT_MIN_DELTA_PCT, DEFAULT_CONFIRM_CHECKS. Those
are still the fallback, and they are the wrong home for the decision. Changing
sensitivity meant editing .env on the host and restarting five services --
which put an operating decision, usually made immediately after being woken by
an alert, behind the one door the person making it cannot open.

A single row, pinned to id = 1 by a check constraint. There is one
deployment-wide default; a second row would be a second answer to a question
that has one, and whichever the query returned first would quietly become the
policy.

Seeded from the current environment rather than from constants, so a
deployment that had tuned those variables keeps exactly the behaviour it had
this morning. A deployment that never set them gets the same 50 / 2.0 / 2 it
was already running on.

Per-target overrides are untouched: monitor_targets already carries its own
nullable min_delta_abs, min_delta_pct and confirm_checks, and build_thresholds
already prefers them. This is only about what they fall back TO.

Revision ID: 0011_alert_defaults
Revises: 0010_missing_since
"""
from __future__ import annotations

import os
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision = "0011_alert_defaults"
down_revision = "0010_missing_since"
branch_labels = None
depends_on = None


def _env(name: str, fallback: str) -> str:
    """The value this deployment is running on today.

    Read from the environment rather than from app.config: a migration must
    not import the application, and Settings would apply its own validation to
    a value we only want to copy forward.
    """
    raw = (os.environ.get(name) or "").strip()
    return raw or fallback


def upgrade() -> None:
    op.create_table(
        "alert_defaults",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("min_delta_abs", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_delta_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("confirm_checks", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="alert_defaults_is_a_singleton"),
        sa.CheckConstraint("min_delta_abs >= 0", name="alert_defaults_abs_not_negative"),
        sa.CheckConstraint(
            "min_delta_pct >= 0 AND min_delta_pct <= 100",
            name="alert_defaults_pct_in_range",
        ),
        sa.CheckConstraint(
            "confirm_checks >= 1 AND confirm_checks <= 10",
            name="alert_defaults_confirm_in_range",
        ),
    )

    op.execute(
        sa.text(
            "INSERT INTO alert_defaults (id, min_delta_abs, min_delta_pct, confirm_checks) "
            "VALUES (1, :abs, :pct, :confirm)"
        ).bindparams(
            # Bound with their real types. Passed as strings they are sent as
            # VARCHAR and Postgres refuses to compare them with numeric.
            sa.bindparam("abs", Decimal(_env("DEFAULT_MIN_DELTA_ABS", "50.0")),
                         type_=sa.Numeric(12, 2)),
            sa.bindparam("pct", Decimal(_env("DEFAULT_MIN_DELTA_PCT", "2.0")),
                         type_=sa.Numeric(6, 2)),
            sa.bindparam("confirm", int(_env("DEFAULT_CONFIRM_CHECKS", "2")),
                         type_=sa.Integer()),
        )
    )


def downgrade() -> None:
    op.drop_table("alert_defaults")
