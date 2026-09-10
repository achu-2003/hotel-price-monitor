"""A link at the bottom of an alert, and a page it can open

An alert says a rate moved. The next question is always "moved to where,
against whom", and the message deliberately does not answer it: printing the
whole grid alongside four moves buries the four -- see the note in
``render_summary``. The answer lives on /comparison, on a screen with room for
a table, and until now there was no way to reach it from a message.

WHY A TABLE AND NOT A SIGNED TOKEN
==================================
The whole WhatsApp message travels inside one URL, against a 2,048-byte cap the
reseller answers with a bare 404. A JWT carrying owner, night and occupancy is
about 250 characters and percent-encodes to 350 -- a fifth of the message
budget, spent pushing real price lines into "and N more on the dashboard".

Sixteen opaque characters cost 45 with the host, and buy revocation as well.

WHO CAN OPEN IT
===============
Anybody holding the link, until it expires. The recipients are phone numbers
with no dashboard account, so a login-walled link would reach nobody -- that is
the trade, and ``expires_at`` is NOT NULL because of it. The row grants read
access to one night of one owner's hotels: it names no user to authenticate as,
and every query behind the page is filtered by ``owner_user_id`` exactly as the
signed-in page filters by the logged-in user.

ONE ROW PER SCOPE, NOT ONE PER MESSAGE. Four alert numbers times a two-hourly
summary is a row every ten minutes, all opening the identical page; the unique
constraint is what makes the writer reuse instead of accumulate.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_comparison_links"
down_revision = "0018_email_kill_switch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comparison_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("check_out", sa.Date(), nullable=False),
        sa.Column("adults", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("baseline_hotel_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: retiring a property should not delete the link
        # and turn every message already quoting it into a dead end. The page
        # falls back to "the first of yours", which is what it does for a
        # baseline that was never chosen.
        sa.ForeignKeyConstraint(
            ["baseline_hotel_id"], ["hotels.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "owner_user_id", "check_in", "check_out", "adults", "baseline_hotel_id",
            name="uq_comparison_links_scope",
        ),
    )
    op.create_index(
        "ix_comparison_links_token", "comparison_links", ["token"], unique=True
    )
    op.create_index(
        "ix_comparison_links_owner_user_id", "comparison_links", ["owner_user_id"]
    )
    # Swept by expiry, so the sweep is an index scan rather than a table scan
    # on a table that grows with every night anybody is alerted about.
    op.create_index(
        "ix_comparison_links_expires_at", "comparison_links", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_comparison_links_expires_at", table_name="comparison_links")
    op.drop_index("ix_comparison_links_owner_user_id", table_name="comparison_links")
    op.drop_index("ix_comparison_links_token", table_name="comparison_links")
    op.drop_table("comparison_links")
