"""Alembic environment.

The database URL comes from the application settings rather than alembic.ini,
so there is exactly one place credentials are configured and none of them are
ever written into a file that gets committed.

Uses the SYNC engine: Alembic has no reason to be async, and mixing the two
here only creates event-loop problems.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.base import Base

# Importing the package registers every model on Base.metadata. Without this,
# autogenerate cheerfully produces a migration that drops all your tables.
import app.db.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Partitions of price_observations are created by a maintenance task at
# runtime, not by migrations. Without this filter, autogenerate would see them
# as stray tables and emit DROP statements for live price history.
_IGNORED_TABLE_PREFIXES = ("price_observations_",)


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and any(name.startswith(p) for p in _IGNORED_TABLE_PREFIXES):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful when a DBA must review and apply changes by hand.
    """
    context.configure(
        url=get_settings().database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_settings().database_url_sync

    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
            # Every migration runs in one transaction, so a failure half way
            # through rolls back rather than leaving a half-migrated schema.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
