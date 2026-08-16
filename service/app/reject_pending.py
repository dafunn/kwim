"""Reject stale unresolved pending review proposals (queue cleanup).

The pre-commit review queue accumulates one `pending_proposals` row per proposal the
gate routes to review. This bulk-resolves the unresolved ones as 'rejected' (not a
hard delete; the audit row stays), filtered by proposer source_kind so it only touches
the intended ones (default `repo_sync` = the code distiller, leaving agent/episodic
proposals alone).

Standalone admin CLI (run via /app/with-secrets.sh, like app.forget). Dry-run by
default; mutation needs --commit, and --confirm-count N (no-TTY) gates on the count
you reviewed.
    python -m app.reject_pending --team <team> [--source-kind repo_sync] \
        [--commit --confirm-count N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .stores.postgres import PostgresStore

log = logging.getLogger("reject_pending")


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    pg = PostgresStore()
    await pg.connect()
    try:
        stats = await pg.pending_stats(args.team, source_kind=args.source_kind)
        print(f"\n=== STALE PENDING - team={args.team} "
              f"source_kind={args.source_kind or 'ANY'} ===")
        print(f"  unresolved: {stats['count']}  (distinct statements: {stats['distinct']} "
              f"-> {stats['count'] - stats['distinct']} duplicate)")
        for s in stats["sample"]:
            print(f"   - {s}")

        if stats["count"] == 0:
            print("\nNothing to reject.")
            return 0
        if not args.commit:
            print("\nDRY-RUN - nothing changed. Re-run with --commit to reject these.")
            return 0
        if args.confirm_count is not None and stats["count"] != args.confirm_count:
            print(f"\nCount mismatch - {stats['count']} pending, --confirm-count="
                  f"{args.confirm_count}. Aborted, nothing changed.")
            return 1

        n = await pg.reject_pending(args.team, source_kind=args.source_kind, reason=args.reason)
        print(f"\nREJECTED {n} pending proposal(s) (resolution='rejected').")
        return 0
    finally:
        await pg.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.reject_pending",
                                 description="Bulk-reject stale pending review proposals")
    ap.add_argument("--team", required=True)
    ap.add_argument("--source-kind", default="repo_sync",
                    help="filter by proposer source_kind (default repo_sync = code "
                    "distiller; pass '' for ANY)")
    ap.add_argument("--reason", default="bulk cleanup: stale/duplicate code distillation")
    ap.add_argument("--commit", action="store_true", help="apply (default: dry-run)")
    ap.add_argument("--confirm-count", type=int, default=None,
                    help="non-interactive: proceed only if the count equals N")
    args = ap.parse_args(argv)
    if args.source_kind == "":
        args.source_kind = None
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
