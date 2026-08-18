#!/usr/bin/env python
"""Create a local .env with freshly generated secrets.

Run once after cloning:

    python scripts/bootstrap_env.py

It copies .env.example, replaces every CHANGE_ME placeholder with a real
random value, and refuses to overwrite an existing .env unless you pass
--force.

Why a script rather than committed defaults: a shared "development" secret has
a habit of reaching production. Generating per-machine values means there is no
default to leak, and nothing secret is ever in git.

The generated .env is for LOCAL DEVELOPMENT. In production, mount these as
Docker secrets in /run/secrets instead; app/config.py reads them from there
automatically and they take priority over the file.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def generate_fernet_key() -> str:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        print("cryptography is not installed. Run: pip install cryptography", file=sys.stderr)
        raise SystemExit(2) from None
    return Fernet.generate_key().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    parser.add_argument("--out", type=Path, default=ROOT / ".env")
    args = parser.parse_args()

    example = ROOT / ".env.example"
    if not example.exists():
        print(f"Missing {example}", file=sys.stderr)
        return 1

    if args.out.exists() and not args.force:
        print(
            f"{args.out} already exists. Refusing to overwrite it "
            f"(pass --force if you really mean to regenerate every secret).\n"
            f"Note: regenerating CREDENTIAL_KEK makes existing stored "
            f"credentials permanently unreadable.",
            file=sys.stderr,
        )
        return 1

    replacements = {
        "SECRET_KEY": secrets.token_urlsafe(64),
        "CREDENTIAL_KEK": generate_fernet_key(),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "ADMIN_PASSWORD": secrets.token_urlsafe(16),
    }

    lines_out: list[str] = []
    for line in example.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in replacements:
                line = f"{key}={replacements[key]}"
        lines_out.append(line)

    args.out.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    try:
        args.out.chmod(0o600)  # no-op on Windows, meaningful on Linux
    except OSError:
        pass

    print(f"Wrote {args.out} with {len(replacements)} generated secrets.")
    print(f"\nFirst admin login:\n  email    : (see ADMIN_EMAIL in .env)")
    print(f"  password : {replacements['ADMIN_PASSWORD']}")
    print("\nChange that password after your first login.")
    print("Back up CREDENTIAL_KEK somewhere other than your database backups:")
    print("without it, stored source credentials cannot be decrypted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
