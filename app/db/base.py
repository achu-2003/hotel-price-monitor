"""SQLAlchemy declarative base and shared mixins."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Predictable constraint names so Alembic autogenerate produces stable,
# reversible migrations instead of database-assigned random names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at / updated_at maintained by the database, not the app.

    Server-side defaults mean a row inserted by a migration, a script, or psql
    still gets correct timestamps.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def pg_enum(enum_cls, name: str):
    """A PostgreSQL enum column that stores the member VALUES.

    SQLAlchemy's default is to store member *names* ("FIXED"), not values
    ("fixed"). That is a trap here: our SQL check constraints, raw queries and
    dashboard filters all speak in values, and the mismatch only shows up at
    runtime as a constraint that never matches.

    Always build enum columns through this helper.
    """
    from sqlalchemy import Enum

    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [member.value for member in cls],
        native_enum=True,
    )
