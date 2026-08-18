"""Tests for redaction and credential encryption.

These protect the requirement that credentials never appear in logs and are
never stored in a readable form.
"""
from __future__ import annotations

import pytest

from app.core.crypto import DecryptionError, decrypt, encrypt
from app.core.redaction import REDACTED, scrub


# ── redaction ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "key",
    ["password", "PASSWORD", "smtp_password", "api_key", "apiKey", "access_token",
     "authorization", "Cookie", "credential_kek", "session_secret", "otp"],
)
def test_sensitive_keys_are_redacted_whatever_they_are_called(key):
    assert scrub({key: "hunter2"})[key] == REDACTED


def test_innocent_keys_pass_through_untouched():
    payload = {"hotel": "Green Valley", "price": 2500, "currency": "INR"}
    assert scrub(payload) == payload


def test_redaction_reaches_arbitrary_depth():
    nested = {"a": {"b": {"c": [{"password": "hunter2", "room": "Deluxe"}]}}}
    result = scrub(nested)
    assert result["a"]["b"]["c"][0]["password"] == REDACTED
    assert result["a"]["b"]["c"][0]["room"] == "Deluxe"


def test_bearer_tokens_are_stripped_from_free_text():
    """Catches the case where a whole header string is logged as a message."""
    line = "Retrying with Authorization: Bearer abcdefghijklmnop1234567890"
    assert "abcdefghijklmnop1234567890" not in scrub(line)


def test_long_opaque_tokens_in_values_are_stripped():
    """Defends against a secret arriving under an innocent-looking key."""
    token = "a" * 50
    assert token not in scrub({"note": f"session={token}"})["note"]


def test_a_short_price_string_is_not_mistaken_for_a_token():
    assert scrub({"note": "price is 2500"}) == {"note": "price is 2500"}


def test_scrub_never_raises_on_hostile_input():
    """Redaction failing must not take down the caller that wanted to log."""

    class Exploding:
        def __repr__(self):
            raise RuntimeError("boom")

    scrub({"x": Exploding()})  # must not raise


def test_deeply_recursive_input_is_truncated_not_fatal():
    payload: dict = {}
    node = payload
    for _ in range(50):
        node["next"] = {}
        node = node["next"]
    scrub(payload)  # must not hit the recursion limit


# ── envelope encryption ──────────────────────────────────────────────
def test_encrypt_decrypt_round_trip():
    secret = "a-booking-engine-password"
    assert decrypt(encrypt(secret)) == secret


def test_ciphertext_does_not_contain_the_plaintext():
    envelope = encrypt("SuperSecretValue123")
    assert "SuperSecretValue123" not in envelope


def test_each_encryption_produces_a_distinct_envelope():
    """A fresh data key per secret: identical inputs must not look identical."""
    assert encrypt("same") != encrypt("same")


def test_tampered_envelope_is_rejected():
    envelope = encrypt("value")
    tampered = envelope[:-6] + "AAAAAA"
    with pytest.raises(DecryptionError):
        decrypt(tampered)


def test_garbage_input_is_rejected_cleanly():
    with pytest.raises(DecryptionError):
        decrypt("not-an-envelope")


def test_decryption_error_message_leaks_nothing():
    """The message goes into logs, so it must stay terse."""
    try:
        decrypt("not-an-envelope")
    except DecryptionError as exc:
        assert str(exc) == "Could not decrypt credential envelope"


def test_unicode_and_symbols_survive_the_round_trip():
    secret = "p@ssw0rd-with-symbols-#!$%^&*()-and-unicode-ü-₹"
    assert decrypt(encrypt(secret)) == secret
