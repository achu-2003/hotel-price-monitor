"""hotels.owner_user_id -- which account added a hotel, and therefore sees it

Until now every account saw every hotel. That is fine for one operator and
wrong the moment there are two: a competitor set is the whole point of this
tool, and one person's watch list is not another's.

``owner_user_id`` makes the hotels table per-account. Every listing filters on
it, so a hotel is visible to the account that created it and to nobody else --
admins included. The role still decides what you may CHANGE; it no longer
decides what you may SEE.

DELIBERATELY NOT BACKFILLED
===========================
Hotels that predate this column keep a NULL owner and are therefore visible to
nobody. That is the requested behaviour: the new account starts with an empty
watch list rather than inheriting whatever was already in the database.

Nothing is deleted and nothing stops being collected -- the workers do not
filter on owner, so the price history of an unowned hotel keeps accruing. To
bring one back onto a screen, give it an owner:

    python scripts/assign_hotel_owner.py --username <name> --hotel-id <id>
    python scripts/assign_hotel_owner.py --username <name> --all-unowned

RESTRICT, not SET NULL: an owner-less hotel is invisible everywhere, so
deleting an account that still owns properties must fail loudly and force a
reassignment rather than silently stranding them.

Revision ID: 0006_hotel_owner
Revises: 0005_current_price
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_hotel_owner"
down_revision = "0005_current_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hotels",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_hotels_owner_user_id", "hotels", ["owner_user_id"])
    op.create_foreign_key(
        "fk_hotels_owner_user_id_users",
        "hotels",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_hotels_owner_user_id_users", "hotels", type_="foreignkey")
    op.drop_index("ix_hotels_owner_user_id", table_name="hotels")
    op.drop_column("hotels", "owner_user_id")
