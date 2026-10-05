"""Forget semantic items: remove :SemanticItem nodes, and the commit_log rows of
directly written ones, by exact id. See docs/DESIGN.md, "Forget".

    python -m kwim_api.forget_semantic --team <team> --ids <id1,id2,...> [--commit]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .forget import preflight
from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore

log = logging.getLogger("forget_semantic")


# --- Reusable core -----------------------------------------------------------

async def plan_forget_semantic(
    falkor: FalkorStore, team: str, item_ids: list[str],
) -> list[dict]:
    """Resolve the target ids to {id, content}; unresolved ids are logged and skipped."""
    plan: list[dict] = []
    for oid in item_ids:
        item = await falkor.get_semantic_for_forget(team, oid)
        if item is None:
            log.warning("not found, skipping: %s", oid)
            continue
        plan.append(item)
    return plan


async def execute_forget_semantic(
    falkor: FalkorStore, pg: PostgresStore, team: str, plan: list[dict],
) -> dict:
    """Delete each planned node, checking it is gone, then its commit_log row (a
    bus-fed item has none). Requires `preflight().ok`."""
    deleted, failed, rows = 0, [], 0
    for p in plan:
        if await falkor.forget_semantic_node(team, p["id"]):
            deleted += 1
            rows += await pg.delete_commit_log(team, p["id"])
        else:
            failed.append(p["id"])
    return {"semantic_items": deleted, "commit_log_rows": rows, "failed": failed}


def _print_plan(team: str, plan: list[dict]) -> None:
    print(f"\nPlan for team {team} - {len(plan)} semantic item(s):")
    for p in plan:
        preview = " ".join(p["content"].split())[:100]
        print(f"  {p['id']}")
        print(f"    {preview}")


# --- CLI (operator batch/one-off; dry-run default + typed confirm) ------------

async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    falkor, pg = FalkorStore(), PostgresStore()
    await falkor.connect()
    await pg.connect()
    try:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        plan = await plan_forget_semantic(falkor, args.team, ids)
        if not plan:
            print("No resolvable semantic items - nothing to forget.")
            return 0
        _print_plan(args.team, plan)

        pre = await preflight(pg, args.team)
        print(f"\nPostgres preflight (role {pre.get('role')}): "
              f"{'ok' if pre.get('ok') else 'CANNOT DELETE'}")
        if not pre.get("ok"):
            print("Aborted - the role cannot delete commit_log rows, so a forgotten "
                  "item would be replayed back by the next rebuild.")
            return 1

        if not args.commit:
            print("\nDRY-RUN - nothing deleted. Re-run with --commit to forget.")
            return 0

        # --confirm-count N, or an interactive typed confirmation, as in kwim_api.forget.
        if args.confirm_count is not None:
            if len(plan) != args.confirm_count:
                print(f"Count mismatch - plan has {len(plan)}, "
                      f"--confirm-count={args.confirm_count}. Aborted, nothing deleted "
                      "(re-run a dry-run to reconcile).")
                return 1
            print(f"--confirm-count {args.confirm_count} matches plan - proceeding.")
        else:
            expect = f"forget {len(plan)} semantic from {args.team}"
            got = input(f'Type exactly to proceed -  {expect}\n> ').strip()
            if got != expect:
                print("Confirmation mismatch - aborted, nothing deleted.")
                return 1

        report = await execute_forget_semantic(falkor, pg, args.team, plan)
        print(f"\nFORGOTTEN: {report}")
        return 1 if report["failed"] else 0
    finally:
        await falkor.close()
        await pg.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kwim_api.forget_semantic",
        description="KWIM semantic forget (destructive hard-removal of :SemanticItem)",
    )
    ap.add_argument("--team", required=True)
    ap.add_argument("--ids", required=True, help="comma-separated SemanticItem ids")
    ap.add_argument("--commit", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--confirm-count", type=int, default=None,
                    help="non-interactive confirm: proceed only if the plan holds exactly N items")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
