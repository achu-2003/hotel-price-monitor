"""The trusted-device cookies from a rate application login

RMS Cloud asks for a one-time code sent by email, and offers "Trust this
device" so it need not ask again. A device, to it, is a cookie. Our browser
context is thrown away after every login, so every login was a new device
and every login would ask. The context's storage state -- cookies and local
storage -- is kept here, sealed with the same envelope as the password, and
handed back to the browser next time.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_rate_app_trusted_device"
down_revision = "0021_rate_application_test"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rate_applications", sa.Column("encrypted_session_state", sa.Text()))
    op.add_column(
        "rate_applications", sa.Column("session_state_saved_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("rate_applications", "session_state_saved_at")
    op.drop_column("rate_applications", "encrypted_session_state")
