"""Pair each of our rooms with the room of theirs it competes with

One paired room was not enough. The owner competes room for room: their
Classic against Sterling's "Classic room", their Deluxe against Sterling's
"Mountain View Classic Room" -- two pairs, priced independently.

Their side is a NAME and not an id. A competitor's room can be re-discovered,
renamed or retired without anyone here noticing, and a dangling id would stop
pairing silently. A name is re-matched every night and says when it stops.

Replaces benchmark_room_type_id, which expressed only the single-room case.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031_benchmark_room_pairs"
down_revision = "0030_benchmark_board_and_room"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repricing_settings",
                  sa.Column("benchmark_room_pairs", JSONB, nullable=False,
                            server_default=sa.text("'{}'::jsonb")))
    op.drop_constraint("fk_repricing_settings_benchmark_room", "repricing_settings", type_="foreignkey")
    op.drop_column("repricing_settings", "benchmark_room_type_id")


def downgrade() -> None:
    op.add_column("repricing_settings", sa.Column("benchmark_room_type_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_repricing_settings_benchmark_room", "repricing_settings", "room_types",
        ["benchmark_room_type_id"], ["id"], ondelete="SET NULL",
    )
    op.drop_column("repricing_settings", "benchmark_room_pairs")
