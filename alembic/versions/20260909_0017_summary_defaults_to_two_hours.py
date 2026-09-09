"""A deployment should send the summary, not wait to be asked for it

``alert_defaults.summary_interval_hours`` defaulted to 0, and 0 means never. So
every deployment installed the two-hourly summary switched off and stayed that
way until somebody found the toggle on the Settings page. That is not a default
-- it is a feature nobody is told about.

The column default becomes 2, which is the cadence the message was designed
around and the one this deployment settled on by hand.

EXISTING ROWS ARE NOT TOUCHED
=============================
Only the default for rows written from here on. A row already holding 0 is left
holding 0, because nothing in the column can tell "never configured" from "an
operator turned this off", and the two want opposite treatment. Flipping the
second on would start sending paid WhatsApp messages that somebody had
deliberately stopped -- an outcome a migration has no business choosing.

There is one ``alert_defaults`` row per deployment and it is created at install,
so in practice this reaches new installs and nothing else. This deployment's row
already says 2.

0 REMAINS MEANINGFUL
====================
It still means never, and the Settings page still offers it. An operator
switching the summary off is making a decision; a fresh install is not.
"""

from alembic import op

revision = "0017_summary_default_2h"
down_revision = "0016_backfill_change_comps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("alert_defaults", "summary_interval_hours", server_default="2")


def downgrade() -> None:
    op.alter_column("alert_defaults", "summary_interval_hours", server_default="0")
