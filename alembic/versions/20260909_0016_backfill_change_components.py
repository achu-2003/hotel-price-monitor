"""Recover each stored move's components from the readings that are still on record

0014 froze a change's components onto its own row, and 0015 gave the series
baselines theirs. Both are forward-looking: a change already in the table when
they ran still has NULLs on both sides, so an alert built from it cannot honour
the "show prices with tax" switch and says so on every line.

That is honest but useless. On this database it is 187 changes, which is every
change there is -- the whole 24-hour digest and the whole two-hour summary read
"excl. tax" under a footer promising "incl. tax".

WHERE THE NUMBERS COME FROM
===========================
Not from arithmetic. ``price_observations`` keeps every reading with its three
components, and a change names two readings. The NEW side is linked directly by
``observation_id_new``. The OLD side is not linked at all -- ``observation_id_old``
has never been populated -- but the reading is still there and is identifiable:
the latest observation on the same ``offer_key``, at or before ``changed_at``,
whose comparison-basis price equals the change's ``old_price``.

That is a lookup of the reading that was in effect when the move was confirmed,
not a reconstruction of it. Where no such reading survives retention, the row
keeps its NULLs and goes on being marked, which is the 0014 contract.

TWO THINGS THAT MAKE THE MATCH SAFE
===================================
No observation on this database carries both ``price_exclusive`` and
``price_inclusive`` -- every source publishes one or the other -- so the
COALESCE below cannot silently pick the component the comparison was not run
on. If a source ever starts publishing both, this migration has already run and
the forward path writes components directly.

Four changes have several candidate readings that disagree, all from 5 Sep when
0012 taught the adapters to split a tax they had previously recorded as one
all-in figure. ORDER BY checked_at DESC takes the most recent, which is both
the richest reading and the one in effect at the change. Ties beyond that are
readings identical in all three components, so the choice among them is not one.

    chg 138  old=9000.00  ex=9000.00 tax=1620.00      @04 Sep 15:22   <- taken
    chg 138  old=9000.00              inc=9000.00     @04 Sep 11:54
    chg 138  old=9000.00              inc=9000.00     @04 Sep 11:22

THE COMPARISON BASIS IS ASSUMED EXCLUSIVE
=========================================
``COALESCE(price_exclusive, price_inclusive)`` mirrors
``adapters/base.py:price_on("exclusive")``, which is ``PRICE_BASIS``'s default
and this deployment's setting. A deployment running on the inclusive basis must
reverse the coalesce before running this, or it will match on the component its
changes were not measured on. Stated here rather than read from config because
a migration that behaves differently depending on an env var is worse than one
that says what it assumes.

IRREVERSIBLE BY DESIGN
======================
The downgrade does not blank the columns: it cannot tell a value this wrote
from one a later check wrote legitimately, and clearing both would destroy the
latter. Same reasoning as 0015.
"""

from alembic import op

revision = "0016_backfill_change_comps"
down_revision = "0015_backfill_baseline"
branch_labels = None
depends_on = None


#: The reading a change moved FROM. Not linked by a column, so it is found:
#: same offer, at or before the move, priced at what the move calls its "was".
#: The lateral lives in a CTE rather than the UPDATE's own FROM: Postgres does
#: not make the update target laterally visible, so ``c`` is unreachable there.
_OLD_SIDE = """
    WITH matched AS (
        SELECT c.id AS change_id,
               o.price_exclusive, o.taxes_fees, o.price_inclusive
          FROM price_changes c
          CROSS JOIN LATERAL (
               SELECT ob.price_exclusive, ob.taxes_fees, ob.price_inclusive
                 FROM price_observations ob
                WHERE ob.offer_key   = c.offer_key
                  AND ob.checked_at <= c.changed_at
                  AND COALESCE(ob.price_exclusive, ob.price_inclusive) = c.old_price
                  AND (ob.price_exclusive IS NOT NULL OR ob.price_inclusive IS NOT NULL)
                ORDER BY ob.checked_at DESC
                LIMIT 1
          ) o
         WHERE c.old_price IS NOT NULL
           AND c.old_price_exclusive IS NULL
           AND c.old_taxes_fees      IS NULL
           AND c.old_price_inclusive IS NULL
    )
    UPDATE price_changes c
       SET old_price_exclusive = m.price_exclusive,
           old_taxes_fees      = m.taxes_fees,
           old_price_inclusive = m.price_inclusive
      FROM matched m
     WHERE m.change_id = c.id
"""

#: The reading a change moved TO. Linked outright on most rows, so that link is
#: used in preference to matching on a price -- it is the same reading either
#: way, but one of them cannot be wrong.
_NEW_SIDE_LINKED = """
    UPDATE price_changes c
       SET new_price_exclusive = o.price_exclusive,
           new_taxes_fees      = o.taxes_fees,
           new_price_inclusive = o.price_inclusive
      FROM price_observations o
     WHERE o.id = c.observation_id_new
       AND c.new_price IS NOT NULL
       AND c.new_price_exclusive IS NULL
       AND c.new_taxes_fees      IS NULL
       AND c.new_price_inclusive IS NULL
       AND (o.price_exclusive IS NOT NULL OR o.price_inclusive IS NOT NULL)
"""

#: The rows the link does not cover, found the way the old side is.
_NEW_SIDE_MATCHED = """
    WITH matched AS (
        SELECT c.id AS change_id,
               o.price_exclusive, o.taxes_fees, o.price_inclusive
          FROM price_changes c
          CROSS JOIN LATERAL (
               SELECT ob.price_exclusive, ob.taxes_fees, ob.price_inclusive
                 FROM price_observations ob
                WHERE ob.offer_key   = c.offer_key
                  AND ob.checked_at >= c.changed_at - INTERVAL '1 day'
                  AND COALESCE(ob.price_exclusive, ob.price_inclusive) = c.new_price
                  AND (ob.price_exclusive IS NOT NULL OR ob.price_inclusive IS NOT NULL)
                ORDER BY ob.checked_at ASC
                LIMIT 1
          ) o
         WHERE c.new_price IS NOT NULL
           AND c.new_price_exclusive IS NULL
           AND c.new_taxes_fees      IS NULL
           AND c.new_price_inclusive IS NULL
    )
    UPDATE price_changes c
       SET new_price_exclusive = m.price_exclusive,
           new_taxes_fees      = m.taxes_fees,
           new_price_inclusive = m.price_inclusive
      FROM matched m
     WHERE m.change_id = c.id
"""


def upgrade() -> None:
    op.execute(_OLD_SIDE)
    op.execute(_NEW_SIDE_LINKED)
    op.execute(_NEW_SIDE_MATCHED)


def downgrade() -> None:
    """Deliberately empty -- see the module docstring."""
