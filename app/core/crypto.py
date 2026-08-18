"""Envelope encryption for credentials stored in the database.

Design: every secret gets its own random data key (DEK). The DEK is wrapped
by the key-encryption key (KEK) that lives ONLY in the environment / Docker
secret, never in the database.

Why bother rather than encrypting directly with the KEK:
  * a DB dump alone is useless without the KEK
  * a single secret can be rotated without touching the others
  * the KEK can be rotated by re-wrapping DEKs, without re-encrypting payloads

⚠️  Losing ``CREDENTIAL_KEK`` means losing every stored credential. Back it up
    separately from the database backups, or you lose both together.
"""
from __future__ import annotations

import base64
import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class DecryptionError(RuntimeError):
    pass


def _kek() -> Fernet:
    raw = get_settings().credential_kek.get_secret_value().encode()
    try:
        return Fernet(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "CREDENTIAL_KEK is not a valid Fernet key. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        ) from exc


def encrypt(plaintext: str) -> str:
    """Return an opaque, storable envelope. Never log the return value."""
    dek = Fernet.generate_key()
    ciphertext = Fernet(dek).encrypt(plaintext.encode())
    envelope = {
        "v": 1,
        "dek": _kek().encrypt(dek).decode(),
        "ct": ciphertext.decode(),
    }
    return base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()


def decrypt(envelope_b64: str) -> str:
    try:
        envelope = json.loads(base64.urlsafe_b64decode(envelope_b64.encode()))
        dek = _kek().decrypt(envelope["dek"].encode())
        return Fernet(dek).decrypt(envelope["ct"].encode()).decode()
    except (InvalidToken, KeyError, ValueError, TypeError) as exc:
        # Deliberately terse: the message goes into logs.
        raise DecryptionError("Could not decrypt credential envelope") from exc


def rewrap(envelope_b64: str, new_kek: str) -> str:
    """Re-wrap an envelope's DEK under a new KEK (key rotation).

    The payload itself is untouched, so rotation is cheap even with many
    stored credentials.
    """
    envelope = json.loads(base64.urlsafe_b64decode(envelope_b64.encode()))
    dek = _kek().decrypt(envelope["dek"].encode())
    envelope["dek"] = Fernet(new_kek.encode()).encrypt(dek).decode()
    return base64.urlsafe_b64encode(json.dumps(envelope).encode()).decode()
