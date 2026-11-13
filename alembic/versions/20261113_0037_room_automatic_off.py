"""A room's automatic repricing can be switched off on one website

13 Nov 2026: the owner wanted to stop the automatic runs moving one room
while the others kept following the market. The channel's switch was all or
nothing. ``auto_off_rooms`` is ``{channel: [room_type_id, ...]}``: an
automatic run holds those rooms on that channel; a manual Apply still works.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_room_automatic_off"
down_revision = "0036_agoda_rooms_by_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repricing_settings",
        sa.Column("auto_off_rooms", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("repricing_settings", "auto_off_rooms")
