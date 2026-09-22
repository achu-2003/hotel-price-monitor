"""Measure the benchmark gap on what the guest pays

The gap was being taken on the rate alone. The owner competes on the line a
guest reads, which is the rate plus the tax the site prints beside it -- and
the two are not a fixed distance apart, because the tax rates differ: on one
night Booking.com printed 5.68% on ours and 5.01% on Sterling's. A hundred
rupees under them before tax was not a hundred rupees under them on screen.

On by default. Off restores the previous pre-tax comparison.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_benchmark_with_tax"
down_revision = "0028_benchmark_undercut"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repricing_settings",
        sa.Column("benchmark_with_tax", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("repricing_settings", "benchmark_with_tax")
