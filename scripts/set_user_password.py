"""Set / reset the password for any email-login user (SA, CA, RM, etc.).

Usage (run from repo root):

    python -m scripts.set_user_password --email sa@rootstalk.in
    python -m scripts.set_user_password --email new@rootstalk.in --create --name "RootsTalk SA"

The password is read from stdin twice (no echo) so it never appears in
shell history. Connects to whatever `DATABASE_URL` resolves to in the
current env, so point it at the right database before running.

Idempotent: if the user already has a password_hash, this overwrites
it. After running, the user can sign in via /auth/admin/login (no
client_short_name) with the email + new password.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import engine
from app.modules.auth.service import hash_password
from app.modules.platform.models import User


MIN_PASSWORD_LENGTH = 8


def _read_password() -> str:
    pw1 = getpass.getpass("New password: ")
    if len(pw1) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        sys.exit("Passwords do not match.")
    return pw1


async def _set_password(email: str, password: str, *, create: bool, name: str) -> None:
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(
            select(User).where(User.email == email)
        )).scalar_one_or_none()

        if user is None:
            if not create:
                sys.exit(
                    f"No user found with email '{email}'. Re-run with "
                    f"--create (and optionally --name) to create one."
                )
            user = User(email=email, name=name or email.split("@")[0])
            db.add(user)
            await db.flush()
            print(f"Created new user '{email}' (id={user.id}).")
        else:
            print(f"Found user '{email}' (id={user.id}).")

        user.password_hash = hash_password(password)
        # Wipe any active session so the new password takes effect immediately
        # everywhere the previous JWT was used.
        user.current_session_id = None
        await db.commit()
        print("Password updated. The previous session (if any) has been invalidated.")
        print(
            "Sign in via POST /auth/admin/login with body "
            f'{{"email": "{email}", "password": "<the new password>"}}.'
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set or reset an email-login user's password.",
    )
    parser.add_argument("--email", required=True, help="User's email address.")
    parser.add_argument(
        "--create", action="store_true",
        help="Create the user if no row exists for this email.",
    )
    parser.add_argument(
        "--name", default="",
        help="Display name for new users (only used with --create).",
    )
    args = parser.parse_args()

    password = _read_password()
    asyncio.run(_set_password(
        email=args.email.strip().lower(),
        password=password,
        create=args.create,
        name=args.name.strip(),
    ))


if __name__ == "__main__":
    main()
