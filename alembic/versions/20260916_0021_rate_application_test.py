"""How the last test of the rate application login went

The page has a Test login button. Its verdict is worth keeping: an owner who
tested yesterday and comes back today should see "logged in, 15 Sep 14:02"
rather than an empty space that asks them to run a browser again to find
out. One attempt per owner, latest only, so it lives on the row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_rate_application_test"
down_revision = "0020_rate_application"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rate_applications", sa.Column("last_test_at", sa.DateTime(timezone=True)))
    op.add_column("rate_applications", sa.Column("last_test_ok", sa.Boolean()))
    op.add_column("rate_applications", sa.Column("last_test_message", sa.Text()))
    op.add_column("rate_applications", sa.Column("last_test_screenshot", sa.Text()))


def downgrade() -> None:
    op.drop_column("rate_applications", "last_test_screenshot")
    op.drop_column("rate_applications", "last_test_message")
    op.drop_column("rate_applications", "last_test_ok")
    op.drop_column("rate_applications", "last_test_at")
