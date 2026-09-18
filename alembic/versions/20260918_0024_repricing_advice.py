"""Repricing advice: the shadow log for the model's second opinion

One row per room, per night, per run, holding what the median rule proposed
and what the language model would have proposed instead -- both as a position
against the market and as the guest price that position produces.

Nothing in this table moves a rate. It is written by ``repricing.run`` when
``advisor_enabled`` is on, and read by whoever has to answer whether the
advisor is actually better than the rule before it is allowed to decide
anything. Rows where the advisor failed are kept deliberately: an advisor
that answers two nights in three is not an improvement, and the empty rows
are the only record of that.

Dropping this table loses the comparison and nothing else -- no rate, no
price series and no alert reads it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_repricing_advice"
down_revision = "0023_repricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repricing_advice",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("room_type_id", sa.Integer(), nullable=True),
        sa.Column("room_name", sa.String(length=200), nullable=True),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("our_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("market_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("competitors", JSONB(), nullable=False, server_default="[]"),
        sa.Column("rule_position_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("rule_target", sa.Numeric(12, 2), nullable=True),
        sa.Column("advisor_position_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("advisor_target", sa.Numeric(12, 2), nullable=True),
        sa.Column("advisor_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("key_factors", JSONB(), nullable=False, server_default="[]"),
        sa.Column("clamped", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_type_id"], ["room_types.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_repricing_advice_owner_time", "repricing_advice", ["owner_user_id", "created_at"]
    )
    op.create_index("ix_repricing_advice_night", "repricing_advice", ["check_in"])


def downgrade() -> None:
    op.drop_index("ix_repricing_advice_night", table_name="repricing_advice")
    op.drop_index("ix_repricing_advice_owner_time", table_name="repricing_advice")
    op.drop_table("repricing_advice")
