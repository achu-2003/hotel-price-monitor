"""A switch for whether a displayed price includes tax

The ten hotels do not agree on what a price is. Six sites quote a room before
tax and state the tax beside it; one quotes before tax and states no tax at
all; three quote a single all-in figure and publish no pre-tax number. Every
series is stored on the configured comparison basis -- exclusive -- so the
three all-in hotels reach the screen through the documented fallback in
``NormalizedOffer.price_on`` and are rendered next to seven pre-tax numbers as
though they were the same kind of thing.

On the matrix that reads as a competitor being 11-15% cheaper or dearer than
they are, with nothing on the page to say which column is which.

So the number shown becomes a choice, made once for the whole deployment on
the settings page, and the components needed to honour it are carried on the
series row.

WHY THE COMPONENTS ARE DENORMALISED HERE
========================================
``price_series`` already carries ``current_price`` for exactly this reason:
the dashboard needs the latest reading and following every row back to
``price_observations`` costs a correlated lookup per room on every page load.
The tax components are needed in the same place, on the same rows, for the
same screens. They are written by ingest alongside ``current_price``.

Existing rows begin NULL and fill on the next successful check -- half an hour
at the shipped interval. Until then a row shows the price it shows today,
which is what it showed yesterday.

THIS DOES NOT CHANGE WHAT IS COMPARED
=====================================
``PRICE_BASIS`` still decides which component the change detector runs on, and
nothing here touches it. Switching the display must not re-baseline a series
or fire an alert about a move that never happened, so the two are kept apart:
this is what a person is shown, that is what the system compares.

Revision ID: 0012_price_with_tax
Revises: 0011_alert_defaults
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_price_with_tax"
down_revision = "0011_alert_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Default false: a deployment upgrading into this keeps the prices it was
    # showing this morning, and the change is something somebody chooses.
    op.add_column(
        "alert_defaults",
        sa.Column(
            "show_prices_with_tax",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    # Nullable, all three. A site that publishes only one of them is the norm
    # rather than the exception here, and a zero would be a claim -- "this room
    # is taxed at nothing" -- where NULL is the truth: it did not say.
    for column in ("last_price_exclusive", "last_taxes_fees", "last_price_inclusive"):
        op.add_column("price_series", sa.Column(column, sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    for column in ("last_price_inclusive", "last_taxes_fees", "last_price_exclusive"):
        op.drop_column("price_series", column)
    op.drop_column("alert_defaults", "show_prices_with_tax")
