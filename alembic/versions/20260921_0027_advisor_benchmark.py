"""The advisor prices against one chosen competitor

The rule's market is every hotel on the hill, and for this owner that is not
how the decision is made: guests choose between their property and Sterling,
and the other nine are noise in the median. One nullable column says which
competitor the ADVISOR is shown -- the rule's own median is untouched, so the
shadow log still compares a model against the rule it is meant to beat.

Backfilled to the owner's active competitor named "Sterling" where they have
one, because that is the benchmark this was asked for. An owner with no such
hotel keeps NULL and keeps the market view.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_advisor_benchmark"
down_revision = "0026_repricing_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repricing_settings", sa.Column("benchmark_hotel_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_repricing_settings_benchmark_hotel", "repricing_settings", "hotels",
        ["benchmark_hotel_id"], ["id"], ondelete="SET NULL",
    )
    # The owner's own Sterling, never another account's: the benchmark feeds
    # a prompt, and a hotel from a different owner would be that account's
    # rates crossing into this one's.
    op.execute(
        """
        UPDATE repricing_settings s
           SET benchmark_hotel_id = h.id
          FROM hotels h
         WHERE h.owner_user_id = s.owner_user_id
           AND h.is_active
           AND NOT h.is_own_property
           AND lower(h.name) = 'sterling'
           AND s.benchmark_hotel_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_repricing_settings_benchmark_hotel", "repricing_settings", type_="foreignkey")
    op.drop_column("repricing_settings", "benchmark_hotel_id")
