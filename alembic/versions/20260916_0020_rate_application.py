"""The application an owner's rates are changed in

Everything before this row reads prices off other people's sites. This is the
first pointer the other way: where the owner's OWN rate is set -- the channel
manager or extranet they log into by hand today -- and the login that opens
it, so that a comparison can eventually end in a new rate rather than a call.

One row per owner, hence the unique constraint: it is the owner's login to
the owner's application, and their properties live inside that application.

The password column holds an envelope from ``app.core.crypto.encrypt`` and is
never read back by any endpoint. The client number and username are plain,
because the page shows them so the owner can see what is set; neither is a
secret alone and both are useless without the password.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_rate_application"
down_revision = "0019_comparison_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("login_url", sa.Text(), nullable=False),
        sa.Column("client_number", sa.String(length=120), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("encrypted_password", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", name="uq_rate_applications_owner"),
    )


def downgrade() -> None:
    op.drop_table("rate_applications")
