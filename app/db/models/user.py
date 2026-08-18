"""Dashboard users and the audit trail of what they changed."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pg_enum
from app.db.models.enums import UserRole


class User(Base, TimestampMixin):
    """An operator of the dashboard.

    ``password_hash`` holds an argon2id hash. There is deliberately no
    plaintext column and no reversible encryption: we never need to read a
    password back, only to verify one.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), default=UserRole.VIEWER, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Login throttling state: 5 failures in 15 minutes locks the account.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set when an admin resets a password; forces a change at next login.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"


class AuditLog(Base):
    """Who changed what, and when.

    Records configuration changes only. Price data is already immutable in
    ``price_observations``, so it needs no audit trail of its own.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity", "entity", "entity_id", "at"),
        Index("ix_audit_user_time", "user_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60), nullable=False)  # create | update | delete
    entity: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(60))

    # Both scrubbed through app.core.redaction before being written, so a
    # credential edit records that it happened without recording the value.
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity}:{self.entity_id}>"


class SourceCredential(Base, TimestampMixin):
    """An encrypted credential for a source that requires an authorised login.

    The value is an envelope produced by ``app.core.crypto.encrypt``: a random
    data key encrypts the secret, and the KEK from the environment wraps that
    data key. A database dump alone is therefore useless.

    Write-only from the dashboard: an operator can set a credential but never
    read one back.
    """

    __tablename__ = "source_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. "username"
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)

    # Encrypted Playwright storage_state, reused until it expires.
    is_session_state: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Re-auth attempts are capped (2/hour) so a wrong password becomes an
    # alert rather than a lockout or a hammering loop.
    reauth_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reauth_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # Never include the value, not even truncated.
        return f"<SourceCredential source={self.source_id} label={self.label!r}>"
