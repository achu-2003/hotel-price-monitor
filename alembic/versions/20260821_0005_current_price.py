"""price_series.current_price -- the latest observed price, for display

The dashboard showed ``last_price``, which is the CONFIRMED baseline the change
detector compares against, not the price the hotel is currently asking. Those
diverge by design: a move that misses the alert threshold (default: must clear
both 50 rupees and 2%) is recorded in ``price_observations`` but deliberately
leaves ``last_price`` alone, so that successive small drifts accumulate against
one fixed point rather than each being waved through relative to the last.

Correct for alerting, wrong on a screen. A 2.8% drop that missed the 50-rupee
floor never reached the dashboard, and because nothing but a threshold-clearing
move resets the baseline, the gap between the screen and the hotel's own
booking page only ever widened.

``current_price`` is written on every check, unconditionally, and is what the
dashboard and API now read. ``last_price`` keeps its meaning untouched, so the
alerting behaviour this project deliberately tuned is unaffected.

Backfilled from the newest ``price_observations`` row per series, on the same
basis the series is stored on, so history is correct the moment this lands
rather than after the next sweep.

Revision ID: 0005_current_price
Revises: 0004_suppression_ops
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_current_price"
down_revision = "0004_suppression_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_series",
        sa.Column("current_price", sa.Numeric(12, 2), nullable=True),
    )

    # Backfill from the newest observation per series, reading the column that
    # matches the basis the series is recorded on. COALESCE mirrors
    # NormalizedOffer.price_on: a source that publishes only one side of the
    # tax still yields a usable number rather than a NULL.
    op.execute(
        """
        UPDATE price_series ps
        SET current_price = latest.price
        FROM (
            SELECT DISTINCT ON (o.offer_key)
                   o.offer_key,
                   CASE WHEN s.last_price_basis = 'exclusive'
                        THEN COALESCE(o.price_exclusive, o.price_inclusive)
                        ELSE COALESCE(o.price_inclusive, o.price_exclusive)
                   END AS price
            FROM price_observations o
            JOIN price_series s ON s.offer_key = o.offer_key
            ORDER BY o.offer_key, o.checked_at DESC
        ) AS latest
        WHERE ps.offer_key = latest.offer_key
        """
    )

    # Any series with no observation at all (or an unpriced one) falls back to
    # the confirmed baseline: stale, but never NULL where a price is expected.
    op.execute(
        "UPDATE price_series SET current_price = last_price WHERE current_price IS NULL"
    )


def downgrade() -> None:
    op.drop_column("price_series", "current_price")
