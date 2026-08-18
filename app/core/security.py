"""Password hashing, JWTs, and session signing for the admin dashboard."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import get_settings

# argon2id with library defaults: memory-hard, resistant to GPU cracking.
_hasher = PasswordHasher()

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plain)
        return True
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True when argon2 parameters have been raised since this hash was made."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except (InvalidHashError, ValueError):
        return True


def create_access_token(
    subject: str,
    role: str,
    expires_minutes: int | None = None,
    *,
    must_change_password: bool = False,
) -> str:
    """Signed token for the API (short-lived) or the session cookie (longer).

    ``chg`` carries "this password must be changed before anything else". It
    is a claim rather than a database lookup so the dashboard can enforce it on
    every request for the cost of one signature check — a per-request query
    would be paid on every page load forever, to catch a state that is true
    once in an account's lifetime.
    """
    settings = get_settings()
    expires = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expires),
        "typ": "access",
        "chg": must_change_password,
    }
    return jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Raises ``jwt.PyJWTError`` on any invalid, expired, or tampered token."""
    return jwt.decode(
        token,
        get_settings().secret_key.get_secret_value(),
        algorithms=[ALGORITHM],
    )
