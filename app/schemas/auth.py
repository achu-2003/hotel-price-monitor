"""Login, tokens, and the current user.

There is no schema here that carries a password hash, and none that lets a
client set a role on itself. Both omissions are the point.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.db.models.enums import UserRole
from app.schemas.common import ORMModel


class LoginIn(ORMModel):
    """A sign-in name and a password.

    ``username`` is deliberately a plain string. It was ``EmailStr``, which
    rejected any account name that was not a deliverable address -- including
    the ones an operator is most likely to choose. Nothing is ever sent to
    this value, so validating it as mail was a constraint with no benefit
    behind it.

    Lower-cased on the way in so that the same account is reachable however
    it was typed. The password is NOT length-checked here: this is the
    verification path, and a minimum applies to setting a password, never to
    offering one. Enforcing it here would only tell an attacker that short
    guesses are not worth making.
    """

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class TokenOut(ORMModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole


class UserOut(ORMModel):
    id: int
    username: str
    full_name: str | None
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None


class UserCreate(ORMModel):
    username: str = Field(min_length=1, max_length=255)
    full_name: str | None = None
    password: str = Field(
        min_length=12,
        max_length=256,
        description="Twelve characters minimum. Length beats complexity rules, "
                    "which mostly produce P@ssw0rd1.",
    )
    role: UserRole = UserRole.VIEWER


class PasswordChangeIn(ORMModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
