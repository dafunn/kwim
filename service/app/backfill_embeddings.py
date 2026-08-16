"""Backfill missing :Fact embeddings - repair the semantic-search blind spot.

Every committed fact is supposed to carry a vector so `knowledge/search` and the
semantic half of `memory/context` can find it. Two ways a fact ends up without one:

  - the gate's embedding screen fails open when the embedder is unavailable
    (`gate._screen_fact` returns (None, None)), so the fact commits correctly but
    with no vector;
  - it committed before the :Fact vector index existed.

Either way the fact is live, governed and returned by `knowledge/query` - but it
cannot match a semantic search, which reads as "we know nothing about that". A
full `app.rebuild` re-embeds as a side effect; this is the targeted, non-destructive
alternative: it only ever adds the `embedding` property to facts that lack one, and
touches no statement, status, edge or commit_log row.

Safe to re-run - already-embedded facts are not selected.

Run (operator, privileged creds via the usual KWIM_* env / with-secrets.sh):
    python -m app.backfill_embeddings --team <team> [--commit]
    python -m app.backfill_embeddings --all-teams [--commit]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .config import settings
from .embedder import Embedder
from .stores.falkor import FalkorStore

log = logging.getLogger("backfill_embeddings")

# Reuse the rebuild batch size - same embedder, same shape of work.
_EMBED_BATCH = settings.embed_batch


# --- Reusable core -----------------------------------------------------------

async def plan_backfill(
    falkor: FalkorStore, team: str, limit: int,
) -> tuple[list[dict], list[dict]]:
    """Split the un-embedded current facts into (embeddable, skipped).

    A fact with a blank statement has nothing to embed; it is reported rather than
    silently counted as done, because it will keep reappearing on every run.
    """
    rows = await falkor.facts_missing_embedding(team, limit=limit)
    embeddable = [r for r in rows if r["statement"].strip()]
    skipped = [r for r in rows if not r["statement"].strip()]
    return embeddable, skipped


async def execute_backfill(
    falkor: FalkorStore, embedder: Embedder, team: str, plan: list[dict],
) -> dict:
    """Embed each statement and attach the vector in place, batched.

    A failed batch is logged and skipped rather than aborting the run - the next
    invocation picks those facts up again, since they still have no embedding.
    """
    embedded, failed = 0, []
    for i in range(0, len(plan), _EMBED_BATCH):
        batch = plan[i : i + _EMBED_BATCH]
        try:
            vectors = await embedder.embed([r["statement"] for r in batch])
        except Exception as exc:
            log.warning("embed failed for batch at offset %d (%d facts): %s",
                        i, len(batch), exc)
            failed.extend(r["id"] for r in batch)
            continue
        for row, vec in zip(batch, vectors):
            if await falkor.set_fact_embedding(team, row["id"], vec):
                embedded += 1
            else:
                log.warning("write verify failed, fact still un-embedded: %s", row["id"])
                failed.append(row["id"])
    return {"embedded": embedded, "failed": failed}


def _print_plan(team: str, plan: list[dict], skipped: list[dict]) -> None:
    print(f"\n[{team}] {len(plan)} fact(s) missing an embedding:")
    for p in plan[:20]:
        preview = " ".join(p["statement"].split())[:100]
        print(f"  {p['id']}")
        print(f"    {preview}")
    if len(plan) > 20:
        print(f"  ... and {len(plan) - 20} more")
    if skipped:
        print(f"  SKIPPED (blank statement, nothing to embed): {len(skipped)}")
        for s in skipped:
            print(f"    {s['id']}")


# --- CLI ----------------------------------------------------------------------

async def backfill_team(
    falkor: FalkorStore, embedder: Embedder | None, team: str, limit: int, commit: bool,
) -> int:
    """Backfill one team. Returns the number of facts left un-embedded afterwards."""
    plan, skipped = await plan_backfill(falkor, team, limit)
    if not plan and not skipped:
        print(f"\n[{team}] All current facts are embedded - nothing to do.")
        return 0
    _print_plan(team, plan, skipped)

    if not commit:
        print(f"[{team}] DRY-RUN - nothing written. Re-run with --commit to backfill.")
        return len(plan)

    assert embedder is not None  # --commit always constructs one
    report = await execute_backfill(falkor, embedder, team, plan)
    print(f"[{team}] BACKFILLED: {report}")
    return len(report["failed"])


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    falkor = FalkorStore()
    await falkor.connect()
    embedder = Embedder() if args.commit else None
    try:
        if args.all_teams:
            # Postgres is only needed to enumerate teams; the backfill itself is
            # graph-only, so a single --team run never touches it.
            from .stores.postgres import PostgresStore
            pg = PostgresStore()
            await pg.connect()
            try:
                teams = await pg.list_team_schemas()
            finally:
                await pg.close()
            if settings.universe_graph not in teams:
                teams.append(settings.universe_graph)
        else:
            teams = [args.team]

        outstanding = 0
        for team in teams:
            try:
                outstanding += await backfill_team(
                    falkor, embedder, team, args.limit, args.commit)
            except Exception as exc:
                log.error("[%s] failed: %s", team, exc)
                outstanding += 1
        return 1 if (outstanding and args.commit) else 0
    finally:
        if embedder is not None:
            await embedder.close()
        await falkor.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="app.backfill_embeddings",
        description="Attach missing embeddings to committed facts (non-destructive)",
    )
    ap.add_argument("--team")
    ap.add_argument("--all-teams", action="store_true",
                    help="every team with a commit_log, plus the universe graph")
    ap.add_argument("--limit", type=int, default=1000,
                    help="max facts to consider per team per run (default: 1000)")
    ap.add_argument("--commit", action="store_true",
                    help="actually embed and write (default: dry-run)")
    args = ap.parse_args(argv)
    if not args.team and not args.all_teams:
        ap.error("Need --team or --all-teams")
    if args.team and args.all_teams:
        ap.error("Cannot use both --team and --all-teams")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
