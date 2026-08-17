"""Forget-semantic - governed hard-removal of :SemanticItem nodes.

Destructive and irreversible, and the only removal path semantic memory has.
`forget.py` covers governed objects (facts/rules); this covers the other half:

  - the HTTP API exposes only GET/POST on /v1/memory/semantic - no delete verb;
  - `falkor.forget_node` hardcodes label = "Fact" | "Rule", so `kwim_api.forget`
    cannot reach a :SemanticItem;
  - `POST /v1/memory/semantic` upserts on a caller-supplied `id`, so a chunk can
    be replaced - but a chunk that should no longer exist at all (a retired
    runbook section, a mis-seeded or leaked chunk) had no way out before this.

Simpler than `forget.py` by design: `memory_semantic_write` calls
`materialize_semantic` and nothing else - no commit_log row, no Postgres record,
no Evidence edges - so the graph node is the whole object. There is nothing to
half-forget, hence no Postgres preflight and no shared-evidence guard.

Targets are exact ids only. There is deliberately no `--select` batch mode: a
pattern that over-matches here cannot be undone by a rebuild, because semantic
items are not derived from commit_log.

Run (operator, privileged creds via the usual KWIM_* env / with-secrets.sh):
    python -m kwim_api.forget_semantic --team <team> --ids <id1,id2,...> [--commit]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .stores.falkor import FalkorStore

log = logging.getLogger("forget_semantic")


# --- Reusable core -----------------------------------------------------------

async def plan_forget_semantic(
    falkor: FalkorStore, team: str, item_ids: list[str],
) -> list[dict]:
    """Resolve the target ids to {id, content}. Ids that don't resolve are logged
    and skipped (absent from the result), matching `plan_forget`."""
    plan: list[dict] = []
    for oid in item_ids:
        item = await falkor.get_semantic_for_forget(team, oid)
        if item is None:
            log.warning("not found, skipping: %s", oid)
            continue
        plan.append(item)
    return plan


async def execute_forget_semantic(
    falkor: FalkorStore, team: str, plan: list[dict],
) -> dict:
    """DETACH DELETE each planned node. `forget_semantic_node` verifies each one
    is actually gone, so a silent partial failure surfaces in the report."""
    deleted, failed = 0, []
    for p in plan:
        if await falkor.forget_semantic_node(team, p["id"]):
            deleted += 1
        else:
            failed.append(p["id"])
    return {"semantic_items": deleted, "failed": failed}


def _print_plan(team: str, plan: list[dict]) -> None:
    print(f"\nPlan for team {team} - {len(plan)} semantic item(s):")
    for p in plan:
        preview = " ".join(p["content"].split())[:100]
        print(f"  {p['id']}")
        print(f"    {preview}")


# --- CLI (operator batch/one-off; dry-run default + typed confirm) ------------

async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    falkor = FalkorStore()
    await falkor.connect()
    try:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        plan = await plan_forget_semantic(falkor, args.team, ids)
        if not plan:
            print("No resolvable semantic items - nothing to forget.")
            return 0
        _print_plan(args.team, plan)

        if not args.commit:
            print("\nDRY-RUN - nothing deleted. Re-run with --commit to forget.")
            return 0

        # Confirmation - destructive + irreversible. Same two paths as kwim_api.forget:
        # --confirm-count for non-interactive runs (aborts if the plan drifted
        # since the dry-run), otherwise an interactive typed confirmation.
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

        report = await execute_forget_semantic(falkor, args.team, plan)
        print(f"\nFORGOTTEN: {report}")
        return 1 if report["failed"] else 0
    finally:
        await falkor.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kwim_api.forget_semantic",
        description="KWIM semantic forget (DESTRUCTIVE hard-removal of :SemanticItem)",
    )
    ap.add_argument("--team", required=True)
    ap.add_argument("--ids", required=True, help="comma-separated SemanticItem ids")
    ap.add_argument("--commit", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--confirm-count", type=int, default=None,
                    help="non-interactive confirm: proceed only if the plan holds exactly N items")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
