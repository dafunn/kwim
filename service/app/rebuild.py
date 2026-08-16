"""FalkorDB rebuild CLI - replay durable Postgres sources into a fresh graph.

Run inside the kwim-service pod (it already has all credentials):

    python -m app.rebuild --team <team> [--skip-semantic] [--in-place] [--yes]
    python -m app.rebuild --all-teams [--skip-semantic] [--yes]
    python -m app.rebuild --team universe [--yes]

The rebuild is Postgres-read-only. It replays commit_log (facts + rules) and
re-embeds episodic events with text (semantic memory). Working memory and
proposal-status TTL keys are not rebuilt - they are ephemeral by design.
"""
import argparse
import asyncio
import json
import sys

from .config import settings
from .embedder import Embedder
from .stores.falkor import FalkorStore, _graph_name
from .stores.postgres import PostgresStore

# Batch size for embedder calls during re-embed (rebuild.embed_batch, env-overridable).
_EMBED_BATCH = settings.embed_batch


async def _replay_commit(
    pg: PostgresStore,
    falkor: FalkorStore,
    team: str,
    graph_name: str | None,
    embedder: "Embedder | None" = None,
) -> None:
    """Replay <team>.commit_log into the target graph.

    When `embedder` is provided (i.e. --skip-semantic not set), each committed
    fact is re-embedded and its :Fact node gains an `embedding` property (
    embeddings are not stored in the log - a model swap means rebuild re-embeds
    with the new model consistently).
    """
    rows = await pg.replay_commit_log(team)
    for row in rows:
        obj_type = row["object_type"]
        op = row["operation"]
        payload = row["payload"]
        provenance = row["provenance"]
        seq = row["seq"]
        obj_id = row["object_id"]

        # payload / provenance may arrive as parsed dicts (psycopg JSONB) or strings.
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(provenance, str):
            provenance = json.loads(provenance)

        if obj_type == "fact" and op == "commit":
            fact = {**payload, "id": obj_id, "commit_seq": seq}
            embedding: list[float] | None = None
            if embedder and payload.get("statement"):
                try:
                    vecs = await embedder.embed([payload["statement"]])
                    embedding = vecs[0]
                except Exception as exc:
                    print(f"  WARNING: failed to embed fact {obj_id}: {exc}", file=sys.stderr)
            await falkor.materialize_fact(team, fact, provenance, graph_name, embedding=embedding)
        elif obj_type == "rule" and op == "commit":
            rule = {**payload, "id": obj_id, "commit_seq": seq, "status": "approved"}
            await falkor.materialize_rule(team, rule, provenance, graph_name)
        elif obj_type == "rule" and op == "reinforce":
            evidence = payload.get("evidence", [])
            await falkor.reinforce_rule(team, obj_id, evidence, seq, graph_name)
        elif obj_type == "rule" and op == "deprecate":
            await falkor.deprecate_rule(team, obj_id, graph_name)
        elif op == "retract":
            await falkor.retract_object(team, obj_type, obj_id, graph_name)
        elif op == "confirm":
            await falkor.confirm_object(
                team, obj_type, obj_id,
                provenance.get("confirmed_by"), provenance.get("confirmed_at"),
                graph_name,
            )
        else:
            print(
                f"  WARNING: unhandled commit_log row "
                f"({obj_type}, {op}) seq={seq}",
                file=sys.stderr,
            )


async def _rebuild_semantic(
    pg: PostgresStore,
    falkor: FalkorStore,
    embedder: Embedder,
    team: str,
    graph_name: str | None,
) -> None:
    """Re-embed episodic events with text into the target graph (batched).

    TODO: items written via POST /v1/memory/semantic are not covered - they have
    no Postgres record to replay. See `main.memory_semantic_write`.
    """
    events = await pg.episodic_with_text(team)
    for i in range(0, len(events), _EMBED_BATCH):
        batch = events[i : i + _EMBED_BATCH]
        texts: list[str] = []
        for ev in batch:
            event_data = ev["event_data"]
            if isinstance(event_data, str):
                event_data = json.loads(event_data)
            texts.append(event_data["text"])

        vectors = await embedder.embed(texts)

        for ev, vec in zip(batch, vectors):
            event_data = ev["event_data"]
            if isinstance(event_data, str):
                event_data = json.loads(event_data)
            text = event_data["text"]
            metadata: dict[str, object] = {
                "event_id": str(ev["id"]),
                "event_type": ev["event_type"],
                "agent_id": ev["agent_id"],
                "session_id": ev["session_id"],
                "occurred_at": str(ev["occurred_at"]) if ev["occurred_at"] else None,
            }
            metadata = {k: v for k, v in metadata.items() if v is not None}
            await falkor.materialize_semantic(
                team,
                {
                    "id": str(ev["id"]),
                    "content": text,
                    "embedding": vec,
                    "metadata": metadata,
                },
                graph_name,
            )


async def _verify_temp_graph(
    falkor: FalkorStore, team: str, expected_min_nodes: int
) -> bool:
    """Sanity-check the temp graph before swapping it live."""
    temp = f"{_graph_name(team)}_rebuild"
    g = falkor._db.select_graph(temp)
    try:
        res = await g.query("MATCH (n) RETURN count(n) AS c")
        count = res.result_set[0][0] if res.result_set else 0
        if count < expected_min_nodes:
            print(
                f"  Temp graph sanity failed: {count} nodes < {expected_min_nodes} expected",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:
        print(f"  Temp graph verification error: {exc}", file=sys.stderr)
        return False


async def _swap_graphs(falkor: FalkorStore, team: str) -> bool:
    """Attempt GRAPH.COPY-based swap. Returns False if the primitive is unavailable."""
    live = _graph_name(team)
    temp = f"{live}_rebuild"
    conn = falkor._db.connection
    try:
        await conn.execute_command("GRAPH.DELETE", live)
        await conn.execute_command("GRAPH.COPY", temp, live)
        await conn.execute_command("GRAPH.DELETE", temp)
        return True
    except Exception as exc:
        # If live was already deleted but COPY failed, the temp graph would
        # linger. Clean it up before falling back so the next rebuild starts
        # from a clean temp graph.
        try:
            await conn.execute_command("GRAPH.DELETE", temp)
        except Exception:
            pass
        print(
            f"  GRAPH.COPY swap failed ({exc}); falling back to in-place clear+replay",
            file=sys.stderr,
        )
        return False


async def _clear_live_graph(falkor: FalkorStore, team: str) -> None:
    """Clear the live graph for an in-place rebuild."""
    g = await falkor._graph(team)
    try:
        await g.query("MATCH (n) DETACH DELETE n")
    except Exception as exc:
        # DETACH DELETE may not be supported; if so, the graph is still usable
        # but may contain stale data. Log and continue.
        print(f"  Warning: could not clear live graph ({exc})", file=sys.stderr)


async def rebuild_team(
    team: str,
    pg: PostgresStore,
    falkor: FalkorStore,
    embedder: Embedder | None,
    in_place: bool,
    yes: bool,
) -> bool:
    """Rebuild one team. Returns True on success, False on failure/skipped."""
    print(f"\n[{team}] Rebuilding...")

    if not yes:
        try:
            resp = input(f"  DESTROY and rebuild kwim_{team}? [y/N] ")
        except EOFError:
            resp = ""
        if resp.lower() not in ("y", "yes"):
            print("  Skipped.")
            return False

    graph_name: str | None = None
    if not in_place:
        graph_name = f"kwim_{team}_rebuild"
        print(f"  Temp graph: {graph_name}")

    # Replay commit_log (fact embedding included when embedder present).
    print("  Replaying commit_log...")
    try:
        await _replay_commit(pg, falkor, team, graph_name, embedder)
    except Exception as exc:
        print(f"  FAILED during commit replay: {exc}", file=sys.stderr)
        return False

    # Semantic re-embed (episodic events with text).
    if embedder:
        print("  Re-embedding episodic events...")
        try:
            await _rebuild_semantic(pg, falkor, embedder, team, graph_name)
        except Exception as exc:
            print(f"  FAILED during semantic re-embed: {exc}", file=sys.stderr)
            return False

    # Swap (if temp graph).
    if not in_place:
        commit_rows = await pg.replay_commit_log(team)
        expected_min = len({r["object_id"] for r in commit_rows})
        print(f"  Verifying temp graph (expect >= {expected_min} nodes)...")
        if not await _verify_temp_graph(falkor, team, expected_min):
            print("  FAILED (temp graph sanity). Live graph untouched.", file=sys.stderr)
            return False

        print("  Swapping temp -> live...")
        swapped = await _swap_graphs(falkor, team)
        if not swapped:
            print("  Falling back to in-place clear + replay into live graph...")
            await _clear_live_graph(falkor, team)
            try:
                await _replay_commit(pg, falkor, team, None, embedder)
            except Exception as exc:
                print(f"  FAILED during fallback replay: {exc}", file=sys.stderr)
                return False
            if embedder:
                try:
                    await _rebuild_semantic(pg, falkor, embedder, team, None)
                except Exception as exc:
                    print(f"  FAILED during fallback semantic re-embed: {exc}", file=sys.stderr)
                    return False

    print(f"  [{team}] OK")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild a FalkorDB graph from durable Postgres sources."
    )
    parser.add_argument("--team", help="Team to rebuild (or 'universe')")
    parser.add_argument("--all-teams", action="store_true", help="Rebuild every team with a commit_log")
    parser.add_argument("--skip-semantic", action="store_true", help="Skip episodic re-embed")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Clear live graph and replay directly (no temp graph)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if not args.team and not args.all_teams:
        parser.error("Need --team or --all-teams")
    if args.team and args.all_teams:
        parser.error("Cannot use both --team and --all-teams")

    pg = PostgresStore()
    falkor = FalkorStore()
    await pg.connect()
    await falkor.connect()

    embedder: Embedder | None = None
    if not args.skip_semantic:
        embedder = Embedder()

    try:
        teams: list[str] = []
        if args.all_teams:
            teams = await pg.list_team_schemas()
            if "universe" not in teams:
                teams.append("universe")
        else:
            teams = [args.team]

        results: dict[str, bool] = {}
        for team in teams:
            try:
                ok = await rebuild_team(
                    team, pg, falkor, embedder, args.in_place, args.yes
                )
                results[team] = ok
            except Exception as exc:
                print(f"  [{team}] FAILED: {exc}", file=sys.stderr)
                results[team] = False

        # Summary
        print("\n--- Rebuild Summary ---")
        for team, ok in results.items():
            status = "OK" if ok else "FAILED"
            print(f"  {team}: {status}")

        return 0 if all(results.values()) else 1
    finally:
        if embedder:
            await embedder.close()
        await falkor.close()
        await pg.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
