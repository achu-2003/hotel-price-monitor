"""Create or reset a dashboard account, by username.

This is the one command that has to work identically in development and in
production, because the whole point of a fixed account is that the same
credential opens both:

    python scripts/create_account.py --username AGS@123 --admin --keep-password

The password comes from ``ACCOUNT_PASSWORD`` in the environment or from an
interactive prompt, never from a command-line argument -- an argument ends up
in shell history and in ``ps`` output on a shared box.

WHY NOT scripts/create_admin.py
===============================
That script exists to bootstrap the FIRST administrator from the values
``scripts/bootstrap_env.py`` generated, and it deliberately forces a password
change at first login because those values are also sitting in a .env file.
That is right for a generated password and wrong for a chosen one: a fixed
credential that demands to be changed the moment it is used is not fixed.

--keep-password is the difference, and it is opt-in rather than the default so
that nobody disarms the forced change by accident.

Deliberately a script rather than something the API does at start-up, for the
same reasons create_admin.py gives: two replicas would race, and a
"create an account if none exists" path in a running web application is a
privilege-escalation hole waiting for the day someone truncates the table.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Running `python scripts/create_account.py` puts scripts/ on sys.path, not the
# project root, so `import app` would fail. Same fix as scripts/probe_site.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import User, UserRole  # noqa: E402
from app.db.session import sync_session  # noqa: E402

RECOMMENDED_LENGTH = 12


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--username",
        help="Sign-in name. Defaults to ADMIN_USERNAME from the environment. "
             "Stored lower-cased; signing in is not case-sensitive.",
    )
    parser.add_argument("--name", default=None, help="Display name")
    parser.add_argument(
        "--admin", action="store_true", help="Give the account the admin role"
    )
    parser.add_argument(
        "--keep-password",
        action="store_true",
        help="Do not force a password change at first login. Use this for a "
             "credential someone chose on purpose and intends to keep.",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Set a new password for an account that already exists",
    )
    args = parser.parse_args()

    settings = get_settings()
    username = (args.username or settings.admin_username or "").strip().lower()
    if not username:
        print("No username. Pass --username or set ADMIN_USERNAME.", file=sys.stderr)
        return 2

    password = _resolve_password(settings)
    if password is None:
        return 2

    # A warning, not a refusal. The minimum is this project's advice; the
    # person running this command with a password in hand has already decided,
    # and a script that silently substituted its own policy for theirs would
    # just be run with the check commented out.
    if len(password) < RECOMMENDED_LENGTH:
        print(
            f"WARNING: that password is {len(password)} characters. This project "
            f"recommends at least {RECOMMENDED_LENGTH} — length beats complexity "
            f"rules, which mostly produce P@ssw0rd1. Proceeding as asked.",
            file=sys.stderr,
        )

    role = UserRole.ADMIN if args.admin else UserRole.VIEWER
    must_change = not args.keep_password

    with sync_session() as session:
        existing = session.scalar(select(User).where(User.username == username))

        if existing is not None:
            if not args.reset_password:
                print(
                    f"{username} already exists. Re-run with --reset-password to set "
                    f"a new password for it."
                )
                return 0
            existing.password_hash = hash_password(password)
            existing.role = role
            existing.is_active = True
            existing.failed_login_count = 0
            existing.locked_until = None
            existing.must_change_password = must_change
            session.commit()
            print(f"Password reset for {username} ({role.value}).")
            return 0

        session.add(
            User(
                username=username,
                full_name=args.name or username,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
                must_change_password=must_change,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        print(f"Created {username} ({role.value}).")
        if must_change:
            print("You will be asked to change this password at first login.")
    return 0


def _resolve_password(settings) -> str | None:
    """From the environment, or an interactive prompt. Never from an argument.

    ACCOUNT_PASSWORD is checked before ADMIN_PASSWORD so that creating a second
    account does not silently reuse the bootstrap administrator's password
    because it happened to still be exported.
    """
    from_env = os.environ.get("ACCOUNT_PASSWORD")
    if from_env:
        return from_env
    if settings.admin_password:
        return settings.admin_password.get_secret_value()

    if not sys.stdin.isatty():
        print(
            "ACCOUNT_PASSWORD is not set and there is no terminal to prompt on. "
            "Set ACCOUNT_PASSWORD, or run this with a TTY.",
            file=sys.stderr,
        )
        return None

    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat: "):
        print("The passwords do not match.", file=sys.stderr)
        return None
    return first


if __name__ == "__main__":
    raise SystemExit(main())
