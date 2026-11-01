"""Create the first PLATFORM_ADMIN account.

The system has no way to bootstrap itself through the API: creating a user
requires `user:create`, which requires a role, which requires a user. This
script breaks that circle exactly once.

    python scripts/create_admin.py --email you@example.com --name "Your Name"

The password is read from the ADMIN_PASSWORD environment variable, or prompted
for if absent — never passed as an argument, where it would land in shell
history and process listings. The account is created with
`must_change_password` set, so the first login forces a change.

This creates the platform owner only. Hospital administrators are invited from
the platform console and choose their own password — nobody types one in for
them, which is why this script no longer offers `--role HOSPITAL_ADMIN`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.core.database import dispose_engine, session_scope
from app.core.exceptions import DomainError
from app.core.security import validate_password_strength
from app.modules.identity import service as identity_service
from app.modules.identity.rbac import Roles
from app.modules.identity.schemas import UserCreate


async def create_admin(*, email: str, full_name: str, password: str) -> int:
    async with session_scope() as session:
        existing = await identity_service.get_user_by_email(session, email)
        if existing is not None:
            print(f"An account already exists for {email}.")
            return 1

        user = await identity_service.create_user(
            session,
            UserCreate(
                email=email,
                full_name=full_name,
                password=password,
                role_codes=[Roles.PLATFORM_ADMIN],
            ),
            hospital_id=None,
            must_change_password=True,
            allow_privileged_roles=True,
        )

    print(f"Created PLATFORM_ADMIN {user.email} ({user.id}).")
    print("The first login will require a password change.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the platform owner account.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, dest="full_name")
    args = parser.parse_args()

    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    try:
        validate_password_strength(password)
    except DomainError as exc:
        print(exc.message)
        return 1

    async def run() -> int:
        try:
            return await create_admin(
                email=args.email,
                full_name=args.full_name,
                password=password,
            )
        except DomainError as exc:
            print(f"{exc.code}: {exc.message}")
            return 1
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
