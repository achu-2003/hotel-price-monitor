"""users.email -> users.username -- sign-in names are not addresses

The login form validated its field as an email address, so a credential like
``ags@123`` was rejected before it ever reached the database: ``123`` is not a
domain, and pydantic's ``EmailStr`` is right about that. The field was wrong
about what it was holding.

Nothing was ever sent to this column. Alerts go to ``recipients``, which is a
separate table precisely because who signs in and who gets told are different
questions -- so the address-shaped validation bought nothing and cost the
operator the account name they wanted.

A rename rather than a new column: there is one identity per account and it
did not change, only what it is allowed to look like. Existing values are
already valid usernames, so every account keeps working and nobody has to be
told a new name.

Revision ID: 0007_username_login
Revises: 0006_hotel_owner
"""
from __future__ import annotations

from alembic import op

revision = "0007_username_login"
down_revision = "0006_hotel_owner"
branch_labels = None
depends_on = None


def _rename_unique(old: str, new: str) -> None:
    """Rename the users unique constraint, whatever it happens to be called.

    This project sets a naming_convention on its MetaData, so 0001's inline
    ``unique=True`` produced ``uq_users_email``. A database built by an older
    revision, or restored by hand, can instead carry Postgres's own default,
    ``users_email_key``. Both are tried, and neither being present is not an
    error: the column rename above is the migration's real work, and failing
    the whole upgrade over a cosmetic constraint name would be a worse
    outcome than a stale one.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{old}') THEN
                ALTER TABLE users RENAME CONSTRAINT {old} TO {new};
            ELSIF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old.replace("uq_users_", "users_")}_key'
            ) THEN
                ALTER TABLE users
                    RENAME CONSTRAINT {old.replace("uq_users_", "users_")}_key
                    TO {new.replace("uq_users_", "users_")}_key;
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    op.alter_column("users", "email", new_column_name="username")
    # The index and the unique constraint carry the old name with them.
    # Renaming them too keeps a psql \d readable and keeps a future
    # autogenerate from proposing a drop-and-recreate of both.
    op.execute("ALTER INDEX IF EXISTS ix_users_email RENAME TO ix_users_username")
    _rename_unique("uq_users_email", "uq_users_username")


def downgrade() -> None:
    _rename_unique("uq_users_username", "uq_users_email")
    op.execute("ALTER INDEX IF EXISTS ix_users_username RENAME TO ix_users_email")
    op.alter_column("users", "username", new_column_name="email")
