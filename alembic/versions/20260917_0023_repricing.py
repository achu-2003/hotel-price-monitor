"""Repricing: the rule, the room mapping, and the record of every move

The comparison page says where the owner's rate sits; the rate application
login says where it is set. These three tables are what turns the one into
the other.

``repricing_settings`` is the owner's rule -- position against the market,
how far a rate may move at once, the floor and ceiling -- and the switch that
lets it run unattended. One row per owner, off by default.

``rms_room_mappings`` says which row of the rate application each of the
owner's rooms is. The booking site and the application name the same room
differently (Booking.com's "Deluxe Double Room" is RMS's CLASSIC, and RMS's
DELUXE is Booking.com's "Standard Double Room"), and a repricer that guessed
would write the right number on the wrong room.

``repricing_actions`` is the audit trail: every proposal, whether it was
applied, held back or only read, with the competitors it rested on and the
picture of the grid afterwards. A rate that moved by itself has to be able to
say why.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_repricing"
down_revision = "0022_rate_app_trusted_device"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repricing_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("auto_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("max_step_pct", sa.Numeric(6, 2), nullable=False, server_default="10"),
        sa.Column("floor_pct", sa.Numeric(6, 2), nullable=False, server_default="30"),
        sa.Column("ceiling_pct", sa.Numeric(6, 2), nullable=False, server_default="50"),
        sa.Column("min_competitors", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("round_to", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("channel", sa.String(length=120), nullable=False, server_default="Booking.com"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", name="uq_repricing_settings_owner"),
    )

    op.create_table(
        "rms_room_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("room_type_id", sa.Integer(), nullable=False),
        sa.Column("rms_room", sa.String(length=200), nullable=False),
        sa.Column("rate_type_ep", sa.String(length=200)),
        sa.Column("rate_type_cp", sa.String(length=200)),
        sa.Column("rate_type_map", sa.String(length=200)),
        sa.Column("floor_amount", sa.Numeric(12, 2)),
        sa.Column("ceiling_amount", sa.Numeric(12, 2)),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_type_id"], ["room_types.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("room_type_id", name="uq_rms_room_mappings_room"),
    )

    op.create_table(
        "repricing_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("room_type_id", sa.Integer()),
        sa.Column("room_name", sa.String(length=200)),
        sa.Column("rms_room", sa.String(length=200)),
        sa.Column("rms_rate_type", sa.String(length=200)),
        sa.Column("plan", sa.String(length=8)),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("our_price", sa.Numeric(12, 2)),
        sa.Column("market_price", sa.Numeric(12, 2)),
        sa.Column("target_price", sa.Numeric(12, 2)),
        sa.Column("competitors", JSONB(), nullable=False, server_default="[]"),
        sa.Column("current_rms", sa.Numeric(12, 2)),
        sa.Column("proposed_rms", sa.Numeric(12, 2)),
        sa.Column("applied_rms", sa.Numeric(12, 2)),
        sa.Column("reason", sa.Text()),
        sa.Column("screenshot_path", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_type_id"], ["room_types.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_repricing_actions_owner_time", "repricing_actions",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_repricing_actions_owner_time", table_name="repricing_actions")
    op.drop_table("repricing_actions")
    op.drop_table("rms_room_mappings")
    op.drop_table("repricing_settings")
