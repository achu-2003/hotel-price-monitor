"""One press to stop the rate emails, without editing every recipient

Each recipient chooses their own channels, which is right for a standing
preference and wrong for the morning somebody's inbox is drowning or a mail
provider starts bouncing every message. The answer to that has to be one press,
not an edit per recipient followed by an edit back.

So a deployment-wide switch. WhatsApp is untouched, which is the point of
putting it on the channel rather than on the recipient: the urgent path keeps
working while the noisy one stops.

DEFAULTS TO ON
==============
True, because that is what every deployment does today and a migration must not
change behaviour by arriving. Somebody turning the emails off is making a
decision; installing this is not.

WHAT IT DOES NOT SILENCE
========================
The operator alerts about the system itself -- a source gone quiet, a repair
that needs a person. Those say monitoring has stopped working, and a switch
labelled "stop email notifications" must not also stop the message that says
nobody is watching the prices any more. See workers/tasks_notify.notify_ops.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_email_kill_switch"
down_revision = "0017_summary_default_2h"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_defaults",
        sa.Column(
            "email_alerts_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_defaults", "email_alerts_enabled")
