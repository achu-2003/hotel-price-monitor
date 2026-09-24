"""One press to stop the rate WhatsApps, beside the one that stops the emails

The same kill switch as 0018, on the other channel: a deployment-wide off over
every recipient's channel choice, with nobody's row edited, so switching it
back on restores them exactly as they were.

DEFAULTS TO ON
==============
For the same reason 0018 did: a migration must not change behaviour by
arriving.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_whatsapp_kill_switch"
down_revision = "0031_benchmark_room_pairs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_defaults",
        sa.Column(
            "whatsapp_alerts_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_defaults", "whatsapp_alerts_enabled")
