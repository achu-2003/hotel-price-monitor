"""Follow the benchmark on Agoda and Goibibo too, each priced off its own site

Two changes, one reason: a channel's RMS rate is worked out from that
channel's site and no other.

``repricing_settings.source_id`` pins the main channel (Booking.com) to the
site its prices come from. Until now the rule took our cheapest price on ANY
site, which was harmless while ASG was tracked on Booking.com alone and wrong
the moment it was tracked on Agoda as well: the Booking.com row in RMS would
have been scaled by Agoda's discount.

Backfilled for a Booking.com channel from the Booking.com source, so the rule
that runs today keeps reading exactly what it reads today. Any other channel
is left NULL, which behaves as before.

``repricing_channel_rules`` holds one row per extra RMS channel. Created
empty, every row off: a migration must not change behaviour by arriving.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0033_repricing_channel_rules"
down_revision = "0032_whatsapp_kill_switch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repricing_settings", sa.Column("source_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_repricing_settings_source", "repricing_settings", "sources",
        ["source_id"], ["id"], ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE repricing_settings rs
           SET source_id = s.id
          FROM sources s
         WHERE rs.source_id IS NULL
           AND lower(rs.channel) = 'booking.com'
           AND s.code = 'www-booking-com'
        """
    )

    op.create_table(
        "repricing_channel_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(120), nullable=False),
        sa.Column("source_id", sa.Integer(),
                  sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auto_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("benchmark_undercut", sa.Numeric(12, 2), nullable=False, server_default="100"),
        sa.Column("benchmark_with_tax", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("benchmark_meal_plan", sa.String(60), nullable=True),
        sa.Column("room_pairs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_user_id", "channel", name="uq_repricing_channel_rule"),
    )


def downgrade() -> None:
    op.drop_table("repricing_channel_rules")
    op.drop_constraint("fk_repricing_settings_source", "repricing_settings", type_="foreignkey")
    op.drop_column("repricing_settings", "source_id")
