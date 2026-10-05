"""Mint, revoke, and list team API keys. Run inside the pod through with-secrets.sh.

    python -m kwim_api.admin_key mint --team acme --label "agent runner" \
            --capability read --capability propose
    python -m kwim_api.admin_key revoke --key-prefix ABCDEFGHIJKL
    python -m kwim_api.admin_key list [--team acme] [--include-revoked]

`mint` prints the key once; a lost key is revoked and replaced. A revocation
from here takes effect within one cache TTL (see _REVOKE_LAG_NOTE).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

import psycopg

from .config import settings
from .keys import generate_key
from .stores.admin import AdminStore

# Capability vocabulary; unknown values are rejected.
CAPABILITIES = ("read", "propose", "review", "promote")

_REVOKE_LAG_NOTE = (
    "Note: this CLI runs outside the service process and cannot clear its in-memory\n"
    "      key cache, so the revoked key may keep authenticating for up to\n"
    "      {ttl:.0f}s (admin.key_cache_ttl_seconds). The console's revoke route\n"
    "      runs in-process and takes effect immediately."
)


async def _mint(store: AdminStore, args: argparse.Namespace) -> int:
    unknown = sorted(set(args.capability) - set(CAPABILITIES))
    if unknown:
        print(f"ERROR: unknown capability {', '.join(unknown)} - "
              f"valid values are {', '.join(CAPABILITIES)}", file=sys.stderr)
        return 1

    expires_at = None
    if args.expires_at:
        try:
            expires_at = datetime.fromisoformat(args.expires_at)
        except ValueError:
            print(f"ERROR: --expires-at {args.expires_at!r} is not ISO 8601", file=sys.stderr)
            return 1

    full_key, key_prefix, key_hash = generate_key()
    row = await store.create_api_key(
        team=args.team, label=args.label, key_prefix=key_prefix, key_hash=key_hash,
        capabilities=sorted(set(args.capability)), expires_at=expires_at,
    )
    caps = ", ".join(row["capabilities"]) or "(none)"
    print(f"\nMinted key for team {row['team']!r}")
    print(f"  label        {row['label']}")
    print(f"  key_prefix   {row['key_prefix']}")
    print(f"  capabilities {caps}")
    print(f"  expires_at   {row['expires_at'] or 'never'}")
    print("\n=== This is the only time the key is shown. Store it now. ===")
    print(f"  {full_key}\n")
    return 0


async def _revoke(store: AdminStore, args: argparse.Namespace) -> int:
    row = await store.revoke_api_key(args.key_prefix)
    if row is None:
        print(f"No live key with prefix {args.key_prefix!r} - nothing to revoke.")
        return 0
    print(f"Revoked key {args.key_prefix} (team {row['team']!r}, label {row['label']!r}).")
    print(_REVOKE_LAG_NOTE.format(ttl=settings.admin_key_cache_ttl_seconds))
    return 0


async def _list(store: AdminStore, args: argparse.Namespace) -> int:
    rows = await store.list_api_keys(team=args.team, include_revoked=args.include_revoked)
    if not rows:
        print("No keys." if args.team is None else f"No keys for team {args.team!r}.")
        return 0
    print(f"\n{'TEAM':<16} {'PREFIX':<14} {'CAPABILITIES':<28} {'LAST USED':<20} LABEL")
    for r in rows:
        caps = ",".join(r["capabilities"]) or "-"
        last = str(r["last_used_at"])[:19] if r["last_used_at"] else "never"
        state = " [REVOKED]" if r["revoked_at"] else ""
        print(f"{r['team']:<16} {r['key_prefix']:<14} {caps:<28} {last:<20} "
              f"{r['label']}{state}")
    print()
    return 0


async def _amain(args: argparse.Namespace) -> int:
    store = AdminStore()
    await store.connect()
    try:
        return await {"mint": _mint, "revoke": _revoke, "list": _list}[args.command](store, args)
    except psycopg.errors.UndefinedTable:
        print("ERROR: kwim_admin schema not found - apply db/admin-schema.sql first.",
              file=sys.stderr)
        return 1
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kwim_api.admin_key",
        description="Mint, revoke, and list team API keys in the kwim_admin store",
        epilog="`mint` prints a secret to stdout - run through with-secrets.sh under "
               "kubectl exec and treat the output as secret material.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    m = sub.add_parser("mint", help="mint a new key for a team")
    m.add_argument("--team", required=True)
    m.add_argument("--label", required=True, help="what this key is for, e.g. 'agent runner'")
    m.add_argument("--capability", action="append", default=[],
                   choices=CAPABILITIES, help="repeatable; omit for a read-only key")
    m.add_argument("--expires-at", help="ISO 8601; omit for no expiry")

    r = sub.add_parser("revoke", help="revoke a key by its prefix")
    r.add_argument("--key-prefix", required=True)

    ls = sub.add_parser("list", help="list keys")
    ls.add_argument("--team", default=None)
    ls.add_argument("--include-revoked", action="store_true")

    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
