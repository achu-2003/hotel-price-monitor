"""Login, tokens, and the current user.

There is no schema here that carries a password hash, and none that lets a
client set a role on itself. Both omissions are the point.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field

from app.db.models.enums import UserRole
from app.schemas.common import ORMModel


class LoginIn(ORMModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenOut(ORMModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None


class UserCreate(ORMModel):
    email: EmailStr
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
