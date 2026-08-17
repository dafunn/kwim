"""Forget - governed hard-removal of facts/rules from memory.

DESTRUCTIVE and IRREVERSIBLE. Completely removes governed objects (facts/rules)
from every store - the FalkorDB node + its embedding, the Postgres commit_log rows,
and the non-shared source episodic events - with no surviving tombstone, so a
`rebuild` cannot re-derive them. Unlike the soft `retract` (status flip), this
deletes data.

Two callers share the core here:
  - the API "Forget" button (`review.py` -> `gate.forget_object` /
    `gate.forget_episodics`), which forgets one object inline on a reviewer click;
  - this standalone CLI, for operator batch/one-off runs, dry-run by default with a
    typed confirmation.

Both paths keep the shared-evidence guard: an episodic that also supports an object
not in the forget set is preserved (override with `--force-shared` / force_shared=True),
and both preflight Postgres DELETE privilege before touching FalkorDB so a permission
failure can't half-forget (node gone, commit_log left -> a rebuild re-derives it).

Run (operator, privileged DB creds via the usual KWIM_* env / with-secrets.sh):
    python -m kwim_api.forget --team <team> --ids <id1,id2,...> [--commit]
    python -m kwim_api.forget --team <team> --select --fact-type code_hub \
        --statement-contains mcp-snapshot [--commit]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore

log = logging.getLogger("forget")


# --- Reusable core (shared by the CLI and the API forget path) ----------------

async def plan_forget(
    falkor: FalkorStore, team: str, object_ids: list[str], *, force_shared: bool,
) -> list[dict]:
    """Build the per-object deletion plan with the shared-evidence guard applied.
    Returns a list of {id, type, status, label, episodics_to_delete, episodics_shared}.
    Objects that don't resolve are logged and skipped (absent from the result)."""
    target_set = set(object_ids)
    plan: list[dict] = []
    for oid in object_ids:
        obj = await falkor.get_object_for_forget(team, oid)
        if obj is None:
            log.warning("not found, skipping: %s", oid)
            continue
        to_delete, shared = [], []
        for eid in obj["evidence"]:
            others = [o for o in await falkor.objects_supported_by(team, eid)
                      if o not in target_set]
            if others and not force_shared:
                shared.append({"episodic": eid, "also_supports": others})
            else:
                to_delete.append(eid)
        plan.append({**obj, "episodics_to_delete": to_delete, "episodics_shared": shared})
    return plan


async def preflight(pg: PostgresStore, team: str) -> dict:
    """Read-only check (no deletes) that the connected Postgres role can DELETE from
    the team's commit_log + episodic_events. Assumes `pg` is already connected. Sets
    `ok` so both the CLI dry-run and the inline forget can refuse a run that would
    half-complete (FalkorDB deleted, Postgres rows left)."""
    try:
        priv = await pg.delete_preflight(team)
        priv["ok"] = bool(priv.get("commit_log") and priv.get("episodic"))
        return priv
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def execute_forget(
    falkor: FalkorStore, pg: PostgresStore, team: str, plan: list[dict],
) -> dict:
    """Apply a plan across both stores. Postgres deletes run per object after its
    FalkorDB node is removed; callers must have confirmed `preflight().ok` first."""
    n_nodes = n_commit = n_epis = 0
    for p in plan:
        await falkor.forget_node(team, p["type"], p["id"])
        n_nodes += 1
        n_commit += await pg.delete_commit_log(team, p["id"])
        if p["episodics_to_delete"]:
            n_epis += await pg.delete_episodic(team, p["episodics_to_delete"])
    return {"objects": n_nodes, "commit_log_rows": n_commit, "episodic_events": n_epis}


async def forget_one(
    falkor: FalkorStore, pg: PostgresStore, team: str, object_id: str, *,
    object_type: str | None = None, force_shared: bool = False,
) -> dict:
    """Inline hard-forget of a single committed object (the API Forget button).

    Resolves + guards + preflights + deletes in one shot (no dry-run - the reviewer
    click IS the confirmation). Returns:
      {"status": "not_found"}                       - no such object
      {"status": "preflight_failed", "preflight": ...} - role can't DELETE; nothing touched
      {"status": "forgotten", type, shared_skipped, objects, commit_log_rows, episodic_events}
    """
    plan = await plan_forget(falkor, team, [object_id], force_shared=force_shared)
    if not plan:
        return {"status": "not_found"}
    pre = await preflight(pg, team)
    if not pre.get("ok"):
        return {"status": "preflight_failed", "preflight": pre}
    report = await execute_forget(falkor, pg, team, plan)
    p = plan[0]
    return {"status": "forgotten", "object_id": object_id, "type": p["type"],
            "shared_skipped": p["episodics_shared"], **report}


async def forget_episodics(
    falkor: FalkorStore, pg: PostgresStore, team: str, episodic_ids: list[str], *,
    force_shared: bool = False,
) -> dict:
    """Inline hard-forget of a rejected/uncommitted proposal's source episodics.

    No graph node exists (nothing was committed), so this only deletes the episodic
    events - after the same shared-evidence guard against committed objects, so an
    event that also supports a live fact/rule is preserved. Returns:
      {"status": "no_delete", episodic_events: 0, shared_skipped}   - nothing to delete
      {"status": "preflight_failed", "preflight": ...}                - role can't DELETE
      {"status": "forgotten", episodic_events: N, shared_skipped}
    """
    to_delete, shared = [], []
    for eid in episodic_ids:
        others = await falkor.objects_supported_by(team, eid)
        if others and not force_shared:
            shared.append({"episodic": eid, "also_supports": others})
        else:
            to_delete.append(eid)
    if not to_delete:
        return {"status": "no_delete", "episodic_events": 0, "shared_skipped": shared}
    pre = await preflight(pg, team)
    if not pre.get("ok"):
        return {"status": "preflight_failed", "preflight": pre}
    n = await pg.delete_episodic(team, to_delete)
    return {"status": "forgotten", "episodic_events": n, "shared_skipped": shared}


# --- CLI (operator batch/one-off; dry-run default + typed confirm) ------------

async def _resolve_targets(
    falkor: FalkorStore, team: str, args: argparse.Namespace,
) -> list[str]:
    """Return the object ids to forget - explicit --ids, or a --select batch."""
    if args.ids:
        return [i.strip() for i in args.ids.split(",") if i.strip()]
    # --select batch
    otypes = [args.type] if args.type else ["fact", "rule"]
    ids: list[str] = []
    for ot in otypes:
        ids += await falkor.select_forget_ids(
            team, object_type=ot, fact_type=args.fact_type,
            source_kind=args.source_kind, status=args.status,
            statement_contains=args.statement_contains)
    return ids


def _print_plan(team: str, plan: list[dict]) -> None:
    print(f"\n=== FORGET PLAN - team={team} - {len(plan)} object(s) ===")
    for p in plan:
        print(f"\n  {p['type']}:{p['id']}  status={p['status']}")
        print(f"    {(p['label'] or '')[:100]}")
        print("    FalkorDB: DETACH DELETE node + orphaned Evidence")
        print(f"    Postgres: delete commit_log rows for {p['id']}")
        if p["episodics_to_delete"]:
            print(f"    Postgres: delete {len(p['episodics_to_delete'])} episodic event(s): "
                  f"{p['episodics_to_delete']}")
        for s in p["episodics_shared"]:
            print(f"    SKIP shared episodic {s['episodic']} (also supports "
                  f"{s['also_supports']}) - use --force-shared to delete anyway")
    print()


def _print_preflight(team: str, pre: dict) -> None:
    print(f"\n=== POSTGRES PREFLIGHT - team={team} ===")
    if "error" in pre:
        print(f"  CONNECTION/CHECK FAILED: {pre['error']}")
    else:
        print(f"  role={pre.get('role')}  DELETE commit_log={pre.get('commit_log')}  "
              f"DELETE episodic_events={pre.get('episodic')}")
    print(f"  -> commit {'CAN' if pre.get('ok') else 'CANNOT'} complete in Postgres")


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    falkor, pg = FalkorStore(), PostgresStore()
    await falkor.connect()
    await pg.connect()
    try:
        ids = await _resolve_targets(falkor, args.team, args)
        if not ids:
            print("No matching objects - nothing to forget.")
            return 0
        plan = await plan_forget(falkor, args.team, ids, force_shared=args.force_shared)
        if not plan:
            print("No resolvable objects - nothing to forget.")
            return 0
        _print_plan(args.team, plan)

        # Read-only PG preflight - runs in both modes so the dry-run surfaces a
        # connection/permission problem before you ever commit.
        pre = await preflight(pg, args.team)
        _print_preflight(args.team, pre)

        if not args.commit:
            print("\nDRY-RUN - nothing deleted. Re-run with --commit to forget.")
            return 0

        if not pre["ok"]:
            print("\nABORT - Postgres preflight failed; refusing to start a forget that "
                  "can't finish (would leave FalkorDB nodes deleted but commit_log rows). "
                  "Supply privileged DB creds and retry.")
            return 1

        # Confirmation - destructive + irreversible. Two paths:
        #   --confirm-count N : non-interactive (playbooks/no-TTY) - proceed only if
        #                       the plan size equals N the operator reviewed in the
        #                       dry-run; a drift in the graph since then aborts.
        #   otherwise         : interactive typed confirmation.
        if args.confirm_count is not None:
            if len(plan) != args.confirm_count:
                print(f"Count mismatch - plan has {len(plan)}, --confirm-count={args.confirm_count}. "
                      "Aborted, nothing deleted (re-run a dry-run to reconcile).")
                return 1
            print(f"--confirm-count {args.confirm_count} matches plan - proceeding.")
        else:
            expect = f"forget {len(plan)} from {args.team}"
            got = input(f'Type exactly to proceed -  {expect}\n> ').strip()
            if got != expect:
                print("Confirmation mismatch - aborted, nothing deleted.")
                return 1

        report = await execute_forget(falkor, pg, args.team, plan)
        print(f"\nFORGOTTEN: {report}")
        return 0
    finally:
        await pg.close()
        await falkor.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kwim_api.forget", description="KWIM forget (DESTRUCTIVE hard-removal)")
    ap.add_argument("--team", required=True)
    sel = ap.add_argument_group("target selection (one of)")
    sel.add_argument("--ids", help="comma-separated object ids")
    sel.add_argument("--select", action="store_true", help="batch by filters below")
    ap.add_argument("--type", choices=["fact", "rule"], help="restrict to one type")
    ap.add_argument("--fact-type", help="(--select) e.g. code_hub, cross_repo_interface")
    ap.add_argument("--source-kind", help="(--select) e.g. repo_sync, distiller")
    ap.add_argument("--status", help="(--select) e.g. retracted, current")
    ap.add_argument("--statement-contains", help="(--select) substring of the object's "
                    "statement/text - the only filter that separates same-metadata "
                    "objects, e.g. --statement-contains mcp-snapshot")
    ap.add_argument("--commit", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--confirm-count", type=int, default=None,
                    help="non-interactive commit (no TTY): proceed only if the plan size "
                    "equals N - the count you reviewed in the dry-run (for playbooks)")
    ap.add_argument("--force-shared", action="store_true",
                    help="delete episodics even if they support other objects (DANGEROUS)")
    args = ap.parse_args(argv)
    if not args.ids and not args.select:
        ap.error("provide --ids <...> or --select with filters")
    if args.select and not (args.fact_type or args.source_kind or args.status
                            or args.statement_contains):
        ap.error("--select needs at least one of "
                 "--fact-type/--source-kind/--status/--statement-contains")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
