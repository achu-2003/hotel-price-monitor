"""Pin the board, and name the room that follows the benchmark

Two facts the rule could not express.

The BOARD: Booking.com sells the same room on two or three plans and the
supplements are not alike. On 24 Sep adding breakfast cost ASG 374 and
Sterling 1,350, so comparing our room-only rate against their breakfast one
reported us 1,269 dearer than we were. Pinning both sides to one board closed
a gap of 2,015 to 346.

The ROOM: the two rooms that actually compete are called "Deluxe Double Room"
and "Classic room". The tier classifier reads names and puts them in
different tiers, correctly -- so the pairing has to be named, not inferred.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_benchmark_board_and_room"
down_revision = "0029_benchmark_with_tax"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repricing_settings", sa.Column("benchmark_meal_plan", sa.String(60), nullable=True))
    op.add_column("repricing_settings", sa.Column("benchmark_room_type_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_repricing_settings_benchmark_room", "repricing_settings", "room_types",
        ["benchmark_room_type_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_repricing_settings_benchmark_room", "repricing_settings", type_="foreignkey")
    op.drop_column("repricing_settings", "benchmark_room_type_id")
    op.drop_column("repricing_settings", "benchmark_meal_plan")
