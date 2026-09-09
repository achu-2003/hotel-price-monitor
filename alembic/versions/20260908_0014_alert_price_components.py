"""The tax components of a price MOVE, so an alert can honour the display switch

The Settings switch decides whether a price is shown with tax. Every screen
honours it; the WhatsApp and email alerts did not, and could not.

WHY THEY COULD NOT
==================
``price_changes`` stores one number per side -- ``old_price`` and
``new_price`` -- both on the configured comparison basis, which here is
exclusive. A message rendered from that row has nothing to add tax to. The
components sit on ``price_series`` (0012), but by the time a change is written
they describe the reading that JUST happened, and the row is overwritten on
every check thereafter. A digest held through quiet hours and rebuilt at 7 AM
would read them hours and several checks later.

So the two sides of the move carry their own components, written once, at the
moment the change is confirmed, and never touched again. A message is then
renderable from its own row forever -- which is what the quiet-hours hold and
the market summary's replay both need.

WHY THE SERIES NEEDS A SECOND SET
=================================
``price_series.last_price_*`` track ``current_price``: every check, including
the ones too small to alert on. ``last_price`` -- the confirmed baseline a
change is measured FROM -- deliberately does not move for those (see the
comment on the column). After a run of sub-threshold drifts the two disagree,
and reading ``last_price_exclusive`` for the OLD side of a change would print
a pre-tax figure from this morning's reading beside a baseline set on Tuesday.

``baseline_price_*`` therefore move in lockstep with ``last_price`` and with
nothing else. Three columns to keep one number honest, which is cheaper than a
message that quotes a price nobody can find.

EXISTING ROWS BEGIN NULL, AND THAT IS THE HONEST STATE
======================================================
Nothing is backfilled. A change confirmed before this ran has no record of
what its tax was, and inventing one -- grossing up at 12% or 18% because
Indian hotel GST is usually one of those -- is exactly the guess
``services/price_display.py`` refuses to make. Those rows render on the number
they have, marked with the basis it is on. Fresh changes carry components from
the next check onward: half an hour at the shipped interval.

Revision ID: 0014_alert_components
Revises: 0013_market_comparison
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_alert_components"
down_revision = "0013_market_comparison"
branch_labels = None
depends_on = None

#: The baseline's components on the series, moving only when the baseline does.
_SERIES_COLUMNS = (
    "baseline_price_exclusive",
    "baseline_taxes_fees",
    "baseline_price_inclusive",
)

#: Both sides of a confirmed move, frozen at the moment it was confirmed.
_CHANGE_COLUMNS = (
    "old_price_exclusive",
    "old_taxes_fees",
    "old_price_inclusive",
    "new_price_exclusive",
    "new_taxes_fees",
    "new_price_inclusive",
)


def upgrade() -> None:
    # Nullable throughout, and never zero. A site that publishes no tax has
    # published no tax; a 0 there would claim the room is taxed at nothing,
    # and the renderer would add it and print a total the page never stated.
    for column in _SERIES_COLUMNS:
        op.add_column("price_series", sa.Column(column, sa.Numeric(12, 2), nullable=True))
    for column in _CHANGE_COLUMNS:
        op.add_column("price_changes", sa.Column(column, sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    for column in reversed(_CHANGE_COLUMNS):
        op.drop_column("price_changes", column)
    for column in reversed(_SERIES_COLUMNS):
        op.drop_column("price_series", column)
