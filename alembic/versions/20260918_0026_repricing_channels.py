"""Repricing on more than one RMS channel

The repricer wrote only the settings' channel (Booking.com). RMS carries the
same rooms under other channels too -- Goibibo, Expedia, the property's own
Book Now button -- and the owner wants to move those as well, but only when
they tick them. Two columns make that possible: which channel an action row
was read from or written to, and which channels the grid listed on the last
visit (so the page can offer them).

Existing action rows keep channel NULL: they were all the settings' channel.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_repricing_channels"
down_revision = "0025_repricing_demand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repricing_actions", sa.Column("channel", sa.String(120), nullable=True))
    op.add_column("repricing_settings",
                  sa.Column("known_channels", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("repricing_settings", "known_channels")
    op.drop_column("repricing_actions", "channel")
