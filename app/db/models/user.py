"""Dashboard users and the audit trail of what they changed."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
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

    ``username`` is a free-form identifier, not an address. It was an email
    column until accounts stopped being addresses -- a sign-in name like
    ``ags@123`` is not deliverable mail, and validating it as though it were
    rejected the credential the operator actually wanted. Nothing is sent
    here; alerts go to ``recipients``, which is a separate table precisely
    because who signs in and who gets told are different questions.

    Stored lower-cased so that signing in is not case-sensitive.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
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
        return f"<User {self.username} ({self.role})>"


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


class RateApplication(Base, TimestampMixin):
    """The application an owner's rates are changed in, and how to log into it.

    The rest of this system READS prices: every adapter opens somebody else's
    booking page and copies what it says. This row is the first thing that
    points the other way -- the channel manager or extranet where the owner's
    own rate is actually set, so that "Sterling is ₹700 under you" can one day
    be answered with a new rate rather than a phone call.

    ONE ROW PER OWNER. It is the owner's login to the owner's application; a
    second property of theirs lives inside that application, not in a second
    row here. Unique on ``owner_user_id`` so the page can PUT without a
    create-or-update dance.

    THE PASSWORD IS WRITE-ONLY. Sealed with ``app.core.crypto.encrypt`` --
    a random data key under the environment's KEK, so a database dump alone is
    useless -- and never returned by any endpoint or shown by any page. The
    client number and username are stored in the clear because the page has
    to show them back for the owner to see what is set; neither is a secret
    on its own, and both are useless without the password.

    ``login_url`` is the page the owner would open by hand. What to do once it
    is open belongs to whichever adapter drives that application, not here.
    """

    __tablename__ = "rate_applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    login_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_number: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False)

    # The last time the login was tried from the dashboard, and how it went.
    # Kept on the row so the page can show it without a second table: there
    # is one application per owner and only the latest attempt matters. The
    # message is the probe's own sentence of evidence; the screenshot is of
    # the page it landed on, taken after the submit, so it never shows the
    # password.
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str | None] = mapped_column(Text)
    last_test_screenshot: Mapped[str | None] = mapped_column(Text)

    # The browser's cookies and local storage after a login that ticked
    # "trust this device", sealed like the password. Applications that ask
    # for a one-time code remember the device that answered it in a cookie;
    # without this the next login is a fresh device and asks again. Cleared
    # whenever the login details change, because a trust earned by one
    # username is not a trust for another.
    encrypted_session_state: Mapped[str | None] = mapped_column(Text)
    session_state_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        # Never the password, not even that there is one.
        return f"<RateApplication owner={self.owner_user_id} url={self.login_url!r}>"
