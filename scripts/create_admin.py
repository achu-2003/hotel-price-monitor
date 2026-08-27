"""Create or reset the first dashboard administrator.

Run once after the first migration:

    docker compose run --rm api python scripts/create_admin.py

With no arguments it uses ``ADMIN_USERNAME`` and ``ADMIN_PASSWORD`` from the
environment, which ``scripts/bootstrap_env.py`` generated. Both can be
overridden on the command line.

For any account after the first, use ``scripts/create_account.py``: it takes a
role, and it can leave a deliberately chosen password in place instead of
forcing a change at first login -- which is right for a chosen password and
wrong for the generated one this script exists to install.

Deliberately a script rather than something the API does at start-up:

* Two API replicas starting together would race to create the same user.
* A "create an admin if none exists" path in a running web application is a
  privilege-escalation hole waiting for the day someone truncates the table.
* Creating an account is a decision someone should make on purpose.

The password is read from the environment or a prompt, never from an argument,
so it does not end up in the shell history or in ``ps``.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path

# Running `python scripts/create_admin.py` puts scripts/ on sys.path, not the
# project root, so `import app` would fail. Same fix as scripts/probe_site.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import User, UserRole  # noqa: E402
from app.db.session import sync_session  # noqa: E402

MIN_PASSWORD_LENGTH = 12


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--username", help="Defaults to ADMIN_USERNAME from the environment"
    )
    parser.add_argument("--name", default="Administrator")
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
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
            f"Length beats complexity rules, which mostly produce P@ssw0rd1.",
            file=sys.stderr,
        )
        return 2

    from sqlalchemy import select

    with sync_session() as session:
        existing = session.scalar(select(User).where(User.username == username))

        if existing is not None:
            if not args.reset_password:
                print(
                    f"{username} already exists. Re-run with --reset-password to set a "
                    f"new password for it."
                )
                return 0
            existing.password_hash = hash_password(password)
            existing.role = UserRole.ADMIN
            existing.is_active = True
            existing.failed_login_count = 0
            existing.locked_until = None
            # Forces a change at next login when the password came from the
            # environment, since that value is also sitting in a .env file.
            existing.must_change_password = True
            print(f"Password reset for {username}.")
            return 0

        session.add(
            User(
                username=username,
                full_name=args.name,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
                must_change_password=True,
                created_at=datetime.now(UTC),
            )
        )
        print(f"Created administrator {username}.")
        print("You will be asked to change this password at first login.")
    return 0


def _resolve_password(settings) -> str | None:
    """From the environment, or an interactive prompt. Never from an argument."""
    if settings.admin_password:
        return settings.admin_password.get_secret_value()

    if not sys.stdin.isatty():
        print(
            "ADMIN_PASSWORD is not set and there is no terminal to prompt on. "
            "Set ADMIN_PASSWORD, or run this with a TTY.",
            file=sys.stderr,
        )
        return None

    first = getpass.getpass("New admin password: ")
    if first != getpass.getpass("Repeat: "):
        print("The passwords do not match.", file=sys.stderr)
        return None
    return first


if __name__ == "__main__":
    raise SystemExit(main())
