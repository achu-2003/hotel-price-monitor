"""How far under the benchmark to sit, in rupees

The benchmark column arrived as advisor-only context. It is now the pricing
rule itself: the owner sets their Booking.com rate a fixed sum under the
competitor they actually compete with, following them up and down. This adds
the sum. Rupees and not a percentage, because "a hundred less than Sterling"
does not become "a hundred and thirty less" when Sterling puts its rate up.

Default 100: the gap this was asked for. Rows with no benchmark ignore it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_benchmark_undercut"
down_revision = "0027_advisor_benchmark"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repricing_settings",
        sa.Column("benchmark_undercut", sa.Numeric(12, 2), nullable=False, server_default="100"),
    )


def downgrade() -> None:
    op.drop_column("repricing_settings", "benchmark_undercut")
