"""Signing in takes a username, and a username is not an email address.

The login field used to be a pydantic ``EmailStr``, which rejected any account
name that was not deliverable mail -- before the request reached the database,
and with a validation error about domains that told the operator nothing
useful about the credential they had been given.

Nothing is ever sent to this value. Alerts go to ``recipients``, a separate
table, precisely because who signs in and who gets told are different
questions. So the address-shaped validation bought nothing and cost real
account names.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import LoginIn


class TestNamesThatAreNotAddresses:
    @pytest.mark.parametrize(
        "username",
        [
            "AGS@123",       # the shape this project actually uses
            "ags",           # no @ at all
            "ops-team",
            "a@b",           # an @ with no dot after it
            "123",
        ],
    )
    def test_they_are_accepted(self, username):
        assert LoginIn(username=username, password="x").username == username.lower()


class TestNormalisation:
    def test_case_is_folded_so_signing_in_is_not_case_sensitive(self):
        assert LoginIn(username="AGS@123", password="x").username == "ags@123"

    def test_surrounding_whitespace_is_dropped(self):
        """Pasted credentials arrive with a trailing space more often than not."""
        assert LoginIn(username="  AGS@123  ", password="x").username == "ags@123"


class TestWhatIsStillRejected:
    def test_an_empty_username(self):
        with pytest.raises(ValidationError):
            LoginIn(username="", password="x")

    def test_a_missing_username(self):
        with pytest.raises(ValidationError):
            LoginIn(password="x")

    def test_a_missing_password(self):
        with pytest.raises(ValidationError):
            LoginIn(username="ags@123")


class TestThePasswordIsNotLengthCheckedOnTheWayIn:
    def test_a_short_password_reaches_verification(self):
        """A minimum applies to SETTING a password, never to offering one.

        Enforcing it here would reject the attempt before the hash comparison
        and tell an attacker that short guesses are not worth making -- and it
        would lock out any account whose password predates the current rule.
        """
        assert LoginIn(username="ags@123", password="AGS123@").password == "AGS123@"
