"""Create the first console operator account.

    python -m kwim_api.admin_bootstrap --username <name> [--display-name <name>]

Run through /app/with-secrets.sh. Refuses if an operator already exists, unless
--force. Reads the password from a confirmed TTY prompt, or from stdin.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

import psycopg

from .keys import hash_password
from .stores.admin import AdminStore


def _read_password() -> str | None:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("ERROR: passwords did not match.", file=sys.stderr)
            return None
        return password
    return sys.stdin.readline().rstrip("\n")


async def _amain(args: argparse.Namespace) -> int:
    store = AdminStore()
    await store.connect()
    try:
        try:
            existing = await store.count_operators()
        except psycopg.errors.UndefinedTable:
            print("ERROR: kwim_admin schema not found - apply db/admin-schema.sql first.",
                  file=sys.stderr)
            return 1

        if existing > 0 and not args.force:
            print(f"ERROR: {existing} operator account(s) already exist. "
                  f"Pass --force to add another.", file=sys.stderr)
            return 1

        password = _read_password()
        if not password:
            print("ERROR: empty password.", file=sys.stderr)
            return 1

        try:
            await store.create_operator(username=args.username,
                                        password_hash=hash_password(password),
                                        display_name=args.display_name)
        except psycopg.errors.UniqueViolation:
            print(f"ERROR: an operator named {args.username!r} already exists.", file=sys.stderr)
            return 1

        print(f"Created operator {args.username!r}.")
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kwim_api.admin_bootstrap",
                                 description="Create the first console operator account")
    ap.add_argument("--username", required=True)
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--force", action="store_true",
                    help="create another operator even if one already exists")
    args = ap.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
