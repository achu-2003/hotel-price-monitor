"""Repricing demand lifts: weekend and sold-out

The rule now keeps the owner's usual gap to the market and lifts it on busy
nights. Two settings say by how much: ``weekend_pct`` on Friday and Saturday
nights (0 by default -- the competitors raise on weekends already, and the
rule follows them), and ``sold_out_pct`` when every competitor of the tier
is sold out, scaled by the share that is.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_repricing_demand"
down_revision = "0024_repricing_advice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repricing_settings",
                  sa.Column("weekend_pct", sa.Numeric(6, 2), nullable=False, server_default="0"))
    op.add_column("repricing_settings",
                  sa.Column("sold_out_pct", sa.Numeric(6, 2), nullable=False, server_default="10"))


def downgrade() -> None:
    op.drop_column("repricing_settings", "sold_out_pct")
    op.drop_column("repricing_settings", "weekend_pct")
