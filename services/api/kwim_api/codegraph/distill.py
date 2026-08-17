"""Code distiller - derive governed Knowledge from the code graph.

Derives a small, high-signal set from kwim_<team>_code - one per-repo architecture
summary (its load-bearing functions) plus cross-repo interfaces - and proposes them
through the existing governance gate (publishes `knowledge.proposed` on the bus, like
POST /v1/knowledge/propose, with source_kind="repo_sync"). The gate screens + commits
them, so they become governed :Fact nodes that survive rebuild and that warm-start
retrieves by `about`. Per-function structural facts are not distilled - that detail
(exact callers, impact) is answered on-demand by the /v1/code/trace read instead.

Run after extraction:
  python -m kwim_api.codegraph.distill --team T --repo R [--min-fan-in N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from ..config import settings
from ..stores.bus import Bus
from ..stores.falkor import FalkorStore

log = logging.getLogger("codegraph.distill")

_PROPOSER = "code-distiller"


async def _sync_fact(
    falkor: FalkorStore, bus: Bus, team: str, *, statement: str,
    fact_type: str, about: list[str], identity_ref: str,
) -> bool:
    """Read the current fact for this identity and propose only if it changed.

    Uses `query_facts(..., about=[identity_ref])` then filters client-side to the
    exact identity (the store's about match is ANY/OR and case-insensitive). An
    unchanged statement means no proposal is published, while a changed statement
    publishes a `supersedes=<current id>` proposal with no `object_id`.

    Returns True iff a proposal was published.
    """
    existing = None
    candidates = await falkor.query_facts(
        team, fact_type=fact_type, status="current", limit=20, about=[identity_ref]
    )
    for c in candidates:
        if identity_ref in (c.get("about") or []):
            existing = c
            break

    if existing is not None and existing["statement"] == statement:
        return False

    pid = str(uuid.uuid4())
    body: dict = {
        "statement": statement,
        "fact_type": fact_type,
        "evidence": [],                 # structural derivation; no episodic evidence
        "about": about,
        "source_kind": "repo_sync",
        "decay_class": "slow",          # structure changes slowly
    }
    if existing is not None:
        body["supersedes"] = existing["id"]

    await falkor.proposal_set(pid, {"id": pid, "object_type": "fact", "status": "accepted"})
    await bus.publish(team, "knowledge.proposed", {
        "proposal_id": pid, "team": team, "object_type": "fact",
        "proposed_by": _PROPOSER, "body": body,
    })
    return True


async def distill_repo(
    falkor: FalkorStore, bus: Bus, team: str, repo: str, *,
    min_fan_in: int | None = None, min_confidence: float | None = None,
) -> dict:
    if min_fan_in is None:
        min_fan_in = settings.cg_min_fan_in
    if min_confidence is None:
        min_confidence = settings.cg_min_confidence
    n_arch = n_iface = 0

    # Architecture orientation - one summary fact per repo naming its load-bearing
    # functions, selected by PageRank importance + cross-community bridging.
    hubs = await falkor.code_hubs(
        team, repo=repo, min_fan_in=min_fan_in, min_confidence=min_confidence)
    if hubs:
        # Lens A: globally important (top PageRank). Lens B: cross-module seams
        # (callers span many communities). Union after the fan-in floor.
        by_pr = sorted(hubs, key=lambda h: h["pagerank"], reverse=True)
        lens_a = by_pr[:settings.cg_arch_top_hubs]
        lens_b = [
            h for h in hubs
            if h["bridged"] >= settings.cg_min_bridged_communities
        ]
        selected = {h["name"]: h for h in lens_a + lens_b}
        top_names = sorted(selected.keys())
        listed = ", ".join(top_names)
        statement = (
            f"{repo} load-bearing functions (high call-centrality / cross-module - "
            f"change with care): {listed}. "
            f"Use the code-graph trace tool for exact callers and impact."
        )
        about = [repo, "architecture"] + top_names
        proposed = await _sync_fact(
            falkor, bus, team, statement=statement, fact_type="code_architecture",
            about=about, identity_ref=repo,
        )
        n_arch = int(proposed)

    # Cross-repo interfaces - callees consumed from another repo.
    ifaces = await falkor.code_cross_repo_interfaces(team, min_confidence=min_confidence)
    for it in ifaces:
        if repo and it["repo"] != repo:
            continue
        consumer_repos = sorted(set(it["consumer_repos"]))
        consumer_list = ", ".join(consumer_repos)
        statement = (
            f"{it['name']} ({it['repo']}/{it['path']}) is a cross-repo interface - "
            f"consumed by caller(s) in: {consumer_list}."
        )
        identity_ref = f"{it['repo']}/{it['path']}"
        about = [it["repo"], it["name"], "interface", identity_ref] + consumer_repos
        proposed = await _sync_fact(
            falkor, bus, team, statement=statement, fact_type="cross_repo_interface",
            about=about, identity_ref=identity_ref,
        )
        n_iface += int(proposed)

    summary = {"team": team, "repo": repo, "architecture_facts": n_arch, "interface_facts": n_iface}
    log.info("codegraph distill done: %s", summary)
    return summary


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    falkor = FalkorStore()
    bus = Bus()
    await falkor.connect()
    await bus.connect()
    try:
        await distill_repo(falkor, bus, args.team, args.repo,
                           min_fan_in=args.min_fan_in)
    finally:
        await bus.close()
        await falkor.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kwim_api.codegraph.distill", description="KWIM code distiller")
    ap.add_argument("--team", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--min-fan-in", type=int, default=None,
                    help="hub threshold (inbound CALLS); default from config (codegraph.min_fan_in)")
    args = ap.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
