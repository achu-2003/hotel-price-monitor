"""The rate application page: four boxes, and what the server makes of them.

The form is served by the dashboard and posted by its generic handler, which
has opinions about what a value is -- digits become a number -- and this is
where those opinions must not reach the database.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.rate_application import RateApplicationIn


def _payload(**overrides):
    base = {
        "login_url": "https://cm.example.com/login",
        "client_number": "00123",
        "username": "ags",
        "password": "hunter2-but-longer",
    }
    return {**base, **overrides}


def test_a_client_number_of_digits_keeps_its_leading_zeros():
    """The form handler sends "00123" as the number 123. The number has no
    zeros to give back, so the server must never see it as a number."""
    assert RateApplicationIn(**_payload(client_number=123)).client_number == "123"
    assert RateApplicationIn(**_payload(client_number="00123")).client_number == "00123"


def test_a_pasted_host_becomes_an_address():
    """What a browser does with the same paste."""
    assert (
        RateApplicationIn(**_payload(login_url="cm.example.com/login")).login_url
        == "https://cm.example.com/login"
    )


def test_an_address_a_browser_cannot_open_is_refused():
    with pytest.raises(ValidationError):
        RateApplicationIn(**_payload(login_url="ftp://cm.example.com"))


def test_the_password_is_optional_so_a_later_save_can_keep_it():
    """The page never shows the stored password back, so it cannot resend
    it. The endpoint decides whether "none sent" is acceptable."""
    assert RateApplicationIn(**_payload(password=None)).password is None


def test_stray_whitespace_around_a_username_is_dropped():
    assert RateApplicationIn(**_payload(username=" ags ")).username == "ags"
