"""Import legacy KWIM_API_KEYS entries into the kwim_admin key store.

    python -m kwim_api.admin_import_keys [--dry-run]      # default: preview only
    python -m kwim_api.admin_import_keys --commit          # mint and write

Mints one new key per entry and prints the mapping, which is secret: run it
through with-secrets.sh under kubectl exec. The dry run prints the plan without
minting. See docs/DESIGN.md, "Team keys".
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import psycopg

from . import auth
from .keys import generate_key
from .stores.admin import AdminStore


def _import_label(key_id: str) -> str:
    """The label an imported row carries, unique per legacy key_id."""
    return f"legacy import - key_id={key_id}"


def _build_plan() -> list[dict]:
    """One entry per KWIM_API_KEYS pair, in order. Warns about legacy key_ids
    shared by several entries, whose capability grant was ambiguous."""
    entries = auth._load_key_map()  # {raw_key: team}, re-read fresh from env - not _KEY_MAP

    by_key_id: dict[str, list[str]] = {}
    for raw_key, team in entries.items():
        by_key_id.setdefault(raw_key[:6], []).append(team)
    for key_id, teams in by_key_id.items():
        if len(teams) > 1:
            print(f"WARNING: legacy key_id {key_id!r} is shared by {len(teams)} entries "
                  f"(teams: {', '.join(sorted(set(teams)))}) - their capability grant was ambiguous. Each imports "
                  f"as its own new key with its own capabilities; check the result below "
                  f"against what you intended each one to have.", file=sys.stderr)

    return [
        {"team": team, "key_id": raw_key[:6],
         "capabilities": sorted(auth._legacy_capabilities(raw_key[:6]))}
        for raw_key, team in entries.items()
    ]


def _print_plan(plan: list[dict]) -> None:
    print(f"\n=== IMPORT PLAN - {len(plan)} legacy key(s) from KWIM_API_KEYS ===")
    for p in plan:
        caps = f"[{', '.join(p['capabilities'])}]" if p["capabilities"] else "(none)"
        print(f"  team={p['team']:<20} legacy_key_id={p['key_id']}  capabilities={caps}")


async def _amain(args: argparse.Namespace) -> int:
    plan = _build_plan()
    if not plan:
        print("No entries in KWIM_API_KEYS - nothing to import.")
        return 0

    _print_plan(plan)

    if not args.commit:
        print("\nDRY-RUN - nothing minted, nothing written. Re-run with --commit to import.")
        return 0

    store = AdminStore()
    await store.connect()
    try:
        # Each row is written before its key is printed, one at a time, so every
        # printed key exists; already-imported entries are skipped on a re-run.
        existing = await store.api_key_labels()
        print("\n=== MINTED KEYS - place these in your secret store now. "
              "This is the only time they are printed. ===")
        minted = skipped = 0
        for p in plan:
            label = _import_label(p["key_id"])
            if label in existing:
                print(f"  team={p['team']:<20} legacy_key_id={p['key_id']}  ->  "
                      f"SKIPPED (already imported)")
                skipped += 1
                continue
            full_key, key_prefix, key_hash = generate_key()
            await store.create_api_key(
                team=p["team"], label=label, key_prefix=key_prefix,
                key_hash=key_hash, capabilities=p["capabilities"],
            )
            minted += 1
            print(f"  team={p['team']:<20} legacy_key_id={p['key_id']}  ->  {full_key}")
        print(f"\nIMPORTED {minted} key(s)" +
              (f", SKIPPED {skipped} already present." if skipped else "."))
        return 0
    except psycopg.errors.UndefinedTable:
        print("ERROR: kwim_admin schema not found - apply db/admin-schema.sql first.",
              file=sys.stderr)
        return 1
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kwim_api.admin_import_keys",
        description="Import legacy KWIM_API_KEYS entries into the kwim_admin key store",
        epilog="This prints freshly-minted key values to stdout - run it through "
              "with-secrets.sh under kubectl exec, and treat the output as secret "
              "material: place it in your secret store immediately.",
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="preview only (default)")
    group.add_argument("--commit", action="store_true", help="mint keys and write them to the store")
    args = ap.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
