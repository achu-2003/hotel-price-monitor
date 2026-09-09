"""Give the existing baselines their components, where the reading still proves them

0014 added ``price_series.baseline_price_*`` and moves them only when the
baseline itself moves. Correct going forward, and it leaves every series that
existed at the time with an empty set until its next confirmed change -- which
for a room that reprices weekly is a week away.

That gap is not cosmetic. Until it closes, a change has an OLD side with no
components and a NEW side with them, and the two cannot be put on the same
basis. Sterling, on this database, mid-gap:

    stored     3,228.57 -> 3,065.10   (-163.47, -5.06%)   the real move
    grossed    3,228.57 -> 3,218.36   ( -10.21, -0.32%)   pre-tax vs all-in

``displayed_move`` now refuses that pair and quotes the stored one instead, so
nobody is shown -0.32%. But the line is then marked and pre-tax, which is
exactly what the switch was built to stop -- and it stays that way for as long
as the gap is open.

WHAT THIS CAN HONESTLY COPY
===========================
``last_price_*`` describe ``current_price``, the most recent reading.
``baseline_*`` must describe ``last_price``, the confirmed baseline. Those are
the SAME reading precisely when ``last_price == current_price`` -- no pending
sub-threshold drift, the baseline is what was last seen -- and there the copy
is not inference, it is the same three numbers under their other name.

Where they differ, a drift is in progress: ``current_price`` has moved off the
baseline and its components belong to the newer reading. Copying there would
print this morning's tax beside Tuesday's price, which is the exact error the
0014 column comment exists to prevent. Those rows are left alone and close the
gap the honest way, on their next confirmed change.

Series with no components at all -- past stay dates last checked before 0012 --
have nothing to copy and are untouched.

On this database that is 122 series repaired, 15 mid-drift left alone.

IRREVERSIBLE BY DESIGN
======================
The downgrade does not blank the columns. It cannot tell a value this wrote
from one a later check wrote legitimately, and clearing both to undo the
former would destroy the latter.
"""

from alembic import op

revision = "0015_backfill_baseline"
down_revision = "0014_alert_components"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IS DISTINCT FROM, not <>: a NULL on either side of <> yields NULL, the
    # row fails the WHERE, and a series whose price is not yet known would be
    # skipped for a reason that has nothing to do with whether it is safe.
    # Here both must be present AND equal, so the plain equality is what is
    # wanted -- stated explicitly so the next reader does not "fix" it.
    op.execute(
        """
        UPDATE price_series
           SET baseline_price_exclusive = last_price_exclusive,
               baseline_taxes_fees      = last_taxes_fees,
               baseline_price_inclusive = last_price_inclusive
         WHERE baseline_price_exclusive IS NULL
           AND baseline_taxes_fees      IS NULL
           AND baseline_price_inclusive IS NULL
           AND last_price    IS NOT NULL
           AND current_price IS NOT NULL
           AND last_price = current_price
           AND (last_price_exclusive IS NOT NULL
                OR last_price_inclusive IS NOT NULL)
        """
    )


def downgrade() -> None:
    """Deliberately empty -- see the module docstring."""
