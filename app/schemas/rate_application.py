"""The application an owner's rates are changed in: what the page sends and gets back."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.common import ORMModel


def _as_text(value: object) -> object:
    """A client number or username typed as digits is still text.

    The dashboard's form handler turns anything that looks like an integer
    into a number before sending it, and a number has no leading zeros: a
    client number of "00123" would arrive as 123 and be saved wrong with no
    error anywhere. Turned back into a string here, so the schema is safe
    whichever client sends to it, and stripped, because a trailing space in a
    username is a login that fails and looks correct on screen.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return value.strip()
    return value


class RateApplicationIn(ORMModel):
    """What the page sends.

    ``password`` is optional on purpose: the form never shows the stored one
    back, so a save that changes only the username must be able to leave the
    password as it is. A first save without one is refused by the endpoint,
    not the schema -- the schema cannot know whether a row already exists.
    """

    login_url: str = Field(min_length=1, max_length=2000)
    client_number: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("client_number", "username", mode="before")
    @classmethod
    def _text(cls, value: object) -> object:
        return _as_text(value)

    @field_validator("login_url", mode="before")
    @classmethod
    def _url(cls, value: object) -> object:
        """A page address, not a bare host or a pasted heading.

        ``https://`` is prepended when it is missing, because that is what a
        browser does with the same paste and refusing it teaches nothing.
        Anything that is not http(s) after that is refused: the login page
        opens in a browser, and a browser does not open ``ftp://``.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text and "://" not in text:
            text = "https://" + text
        if text and not text.lower().startswith(("http://", "https://")):
            raise ValueError("must be a web address starting with http:// or https://")
        return text


class RateApplicationOut(ORMModel):
    """What is stored, less the password.

    ``has_password`` says one is set without saying what it is -- the page
    needs to tell "saved, leave the box empty to keep it" from "not set yet".
    """

    login_url: str
    client_number: str
    username: str
    has_password: bool
    updated_at: datetime
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_message: str | None = None
    has_screenshot: bool = False
    #: When "trust this device" cookies were kept, so the page can say the
    #: next login should not ask for a code.
    session_state_saved_at: datetime | None = None


class LoginTestStatus(ORMModel):
    """Where a login test is.

    ``idle``: none running. ``running``: the browser is signing in.
    ``needs_code``: the application asked for a one-time code and the page
    should show a box for it; ``message`` is the application's own prompt.
    ``done``: ``ok`` is the verdict and ``message`` the evidence.
    """

    status: str
    message: str | None = None
    ok: bool | None = None
    has_screenshot: bool = False
    tested_at: datetime | None = None


class LoginCodeIn(ORMModel):
    """The one-time code, as typed. Digits usually, but not always."""

    code: str = Field(min_length=3, max_length=32)

    @field_validator("code", mode="before")
    @classmethod
    def _text(cls, value: object) -> object:
        # The form handler would send "004521" as 4521; see ``_as_text``.
        return _as_text(value)
