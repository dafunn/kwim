"""Rebuild a team's FalkorDB graph from Postgres: replay commit_log, reapply
fact_verifications, re-embed episodic text. Reads Postgres only. See
docs/DESIGN.md, "The commit log is the source of truth".

    python -m kwim_api.rebuild --team <team> [--skip-semantic] [--in-place] [--yes]
    python -m kwim_api.rebuild --all-teams [--skip-semantic] [--yes]
    python -m kwim_api.rebuild --team universe [--yes]

--skip-semantic skips every embedding, including logged semantic items, which a
later rebuild without it restores.
"""
import argparse
import asyncio
import json
import sys

from .config import settings
from .embedder import Embedder
from .freshness import _to_dt
from .stores.falkor import FalkorStore, _graph_name, live_graph_lock
from .stores.postgres import PostgresStore

# Batch size for embedder calls during re-embed (rebuild.embed_batch, env-overridable).
_EMBED_BATCH = settings.embed_batch


def _epoch_ms(ts) -> int | None:
    """Convert a stored timestamp to FalkorDB's epoch milliseconds, or None if
    it cannot be parsed (the node then gets the store's own timestamp)."""
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    dt = _to_dt(ts)
    return int(dt.timestamp() * 1000) if dt is not None else None


async def _replay_commit(
    pg: PostgresStore,
    falkor: FalkorStore,
    team: str,
    graph_name: str | None,
    embedder: "Embedder | None" = None,
    rows: list[dict] | None = None,
) -> None:
    """Replay <team>.commit_log into the target graph in seq order, restoring each
    node's created_at from committed_at. With `embedder`, facts and logged semantic
    items are embedded. `rows` replays only those (a range read by the caller)."""
    if rows is None:
        rows = await pg.replay_commit_log(team)
    for row in rows:
        obj_type = row["object_type"]
        op = row["operation"]
        payload = row["payload"]
        provenance = row["provenance"]
        seq = row["seq"]
        obj_id = row["object_id"]
        created_at = _epoch_ms(row.get("committed_at"))

        # payload / provenance may arrive as parsed dicts (psycopg JSONB) or strings.
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(provenance, str):
            provenance = json.loads(provenance)

        if obj_type in ("document", "skill"):
            # Documents are projected into kwim_<team>_docs and skills are read
            # from Postgres; neither is in the team graph.
            continue
        if obj_type == "fact" and op == "commit":
            fact = {**payload, "id": obj_id, "commit_seq": seq}
            embedding: list[float] | None = None
            if embedder and payload.get("statement"):
                try:
                    vecs = await embedder.embed([payload["statement"]])
                    embedding = vecs[0]
                except Exception as exc:
                    print(f"  WARNING: failed to embed fact {obj_id}: {exc}", file=sys.stderr)
            await falkor.materialize_fact(team, fact, provenance, graph_name,
                                          embedding=embedding, created_at=created_at)
        elif obj_type == "rule" and op == "commit":
            rule = {**payload, "id": obj_id, "commit_seq": seq, "status": "approved"}
            await falkor.materialize_rule(team, rule, provenance, graph_name,
                                          created_at=created_at)
        elif obj_type == "semantic" and op == "commit":
            # A directly written semantic item; needs the embedder.
            if embedder is None:
                print(f"  WARNING: no embedder - skipping semantic {obj_id} "
                      f"(seq={seq}); re-run without --skip-semantic", file=sys.stderr)
            else:
                try:
                    vecs = await embedder.embed([payload["content"]])
                except Exception as exc:
                    print(f"  WARNING: failed to embed semantic {obj_id}: {exc}",
                          file=sys.stderr)
                else:
                    await falkor.materialize_semantic(
                        team,
                        {"id": obj_id, "content": payload["content"],
                         "embedding": vecs[0], "metadata": payload.get("metadata", {})},
                        graph_name, created_at=created_at,
                    )
        elif op == "amend":
            # Overwrite content; previous_payload is not replayed.
            prev = (provenance.get("previous_payload") or {})
            if obj_type == "fact":
                emb = None
                if embedder and payload.get("statement"):
                    try:
                        emb = (await embedder.embed([payload["statement"]]))[0]
                    except Exception as exc:
                        print(f"  WARNING: failed to embed amended fact {obj_id}: {exc}",
                              file=sys.stderr)
                await falkor.amend_fact(team, obj_id, payload, seq, graph_name,
                                        embedding=emb)
            elif obj_type == "rule":
                await falkor.amend_rule(team, obj_id, payload, seq,
                                        previous_situation=prev.get("situation"),
                                        graph_name=graph_name)
            elif obj_type == "semantic":
                emb = None
                if embedder and payload.get("content"):
                    try:
                        emb = (await embedder.embed([payload["content"]]))[0]
                    except Exception as exc:
                        print(f"  WARNING: failed to embed amended semantic {obj_id}: "
                              f"{exc}", file=sys.stderr)
                await falkor.amend_semantic(team, obj_id, payload,
                                            previous_metadata=prev.get("metadata"),
                                            graph_name=graph_name, embedding=emb)
            else:
                print(f"  WARNING: amend of unknown object_type {obj_type} seq={seq}",
                      file=sys.stderr)
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


async def _apply_verifications(
    pg: PostgresStore, falkor: FalkorStore, team: str, graph_name: str | None,
) -> int:
    """Reapply last_verified_at from <team>.fact_verifications onto the replayed
    current facts. Returns the number stamped."""
    rows = await pg.read_verifications(team)
    applied = 0
    for r in rows:
        if await falkor.reaffirm_fact(
            team, r["fact_id"], verified_at=_epoch_ms(r["last_verified_at"]),
            graph_name=graph_name,
        ):
            applied += 1
    return applied


async def _rebuild_semantic(
    pg: PostgresStore,
    falkor: FalkorStore,
    embedder: Embedder,
    team: str,
    graph_name: str | None,
) -> None:
    """Re-embed episodic events with text into the target graph (batched)."""
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

        for ev, vec in zip(batch, vectors, strict=True):
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
                created_at=_epoch_ms(ev["occurred_at"]),
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


class SwapUnsupported(Exception):
    """This FalkorDB has no GRAPH.COPY."""


async def _swap_graphs(falkor: FalkorStore, team: str) -> bool:
    """Make the temp graph the live one, under the team's live-graph lock so no
    query in this process lands between the delete and the copy: copy live to a
    backup, delete live, copy temp onto its name. If that copy fails, the backup
    is copied back, so the live graph is never left missing; the temp graph is
    kept for another try. Raises SwapUnsupported when GRAPH.COPY does not exist."""
    live = _graph_name(team)
    temp = f"{live}_rebuild"
    backup = f"{live}_prerebuild"
    conn = falkor._db.connection
    async with live_graph_lock(team):
        await _delete_graph(conn, backup)
        try:
            await conn.execute_command("GRAPH.COPY", live, backup)
        except Exception as exc:
            if "unknown command" in str(exc).lower():
                raise SwapUnsupported(str(exc)) from exc
            if "empty key" not in str(exc).lower():   # no live graph yet: nothing to back up
                print(f"  could not back up {live} ({exc}); live graph untouched", file=sys.stderr)
                return False
        try:
            await _delete_graph(conn, live)
            await conn.execute_command("GRAPH.COPY", temp, live)
        except Exception as exc:
            print(f"  GRAPH.COPY swap failed ({exc}); restoring {live} from {backup}", file=sys.stderr)
            try:
                await _delete_graph(conn, live)
                await conn.execute_command("GRAPH.COPY", backup, live)
            except Exception as exc2:
                print(f"  RESTORE FAILED ({exc2}); the previous graph is kept as {backup}",
                      file=sys.stderr)
                return False
            await _delete_graph(conn, backup)
            return False
        falkor._inited.discard(live)
        await _delete_graph(conn, temp)
        await _delete_graph(conn, backup)
        return True


async def _delete_graph(conn, name: str) -> None:
    """GRAPH.DELETE that treats a missing graph as done."""
    try:
        await conn.execute_command("GRAPH.DELETE", name)
    except Exception as exc:
        msg = str(exc).lower()
        if not ("empty key" in msg or "not exist" in msg or "unknown graph" in msg):
            raise


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
        # A temp graph left by an earlier rebuild that did not finish.
        await _delete_graph(falkor._db.connection, graph_name)

    # The commit_log as of now. Rows committed while the rebuild runs are caught
    # up after the swap, so a team that keeps taking writes still converges.
    rows = await pg.replay_commit_log(team)
    snapshot_seq = rows[-1]["seq"] if rows else 0

    # Replay commit_log (fact embedding included when embedder present).
    print(f"  Replaying commit_log (through seq {snapshot_seq})...")
    try:
        await _replay_commit(pg, falkor, team, graph_name, embedder, rows=rows)
    except Exception as exc:
        print(f"  FAILED during commit replay: {exc}", file=sys.stderr)
        return False

    print("  Reapplying fact verifications...")
    try:
        n = await _apply_verifications(pg, falkor, team, graph_name)
        print(f"    {n} fact(s) restamped")
    except Exception as exc:
        print(f"  FAILED during verification reapply: {exc}", file=sys.stderr)
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
        expected_min = len({r["object_id"] for r in rows
                            if r["object_type"] not in ("document", "skill")})
        print(f"  Verifying temp graph (expect >= {expected_min} nodes)...")
        if not await _verify_temp_graph(falkor, team, expected_min):
            print("  FAILED (temp graph sanity). Live graph untouched.", file=sys.stderr)
            return False

        print("  Swapping temp -> live...")
        try:
            swapped = await _swap_graphs(falkor, team)
        except SwapUnsupported as exc:
            # No GRAPH.COPY: rebuild the live graph in place instead.
            print(f"  GRAPH.COPY unavailable ({exc}); clearing and replaying the live graph...")
            await _delete_graph(falkor._db.connection, graph_name)
            await _clear_live_graph(falkor, team)
            try:
                await _replay_commit(pg, falkor, team, None, embedder, rows=rows)
                await _apply_verifications(pg, falkor, team, None)
            except Exception as exc2:
                print(f"  FAILED during in-place replay: {exc2}", file=sys.stderr)
                return False
            if embedder:
                try:
                    await _rebuild_semantic(pg, falkor, embedder, team, None)
                except Exception as exc2:
                    print(f"  FAILED during in-place semantic re-embed: {exc2}", file=sys.stderr)
                    return False
            swapped = True
        if not swapped:
            print(f"  FAILED: could not swap; the rebuilt graph is kept as {graph_name}.",
                  file=sys.stderr)
            return False

        # Catch up rows committed since the snapshot, into the live graph.
        for _ in range(10):
            newer = await pg.replay_commit_log(team, since_seq=snapshot_seq)
            if not newer:
                break
            print(f"  Catching up {len(newer)} row(s) committed during the rebuild...")
            await _replay_commit(pg, falkor, team, None, embedder, rows=newer)
            snapshot_seq = newer[-1]["seq"]
        else:
            print("  WARNING: still catching up after 10 rounds; later rows land through the gate",
                  file=sys.stderr)

    print(f"  [{team}] OK")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild a FalkorDB graph from durable Postgres sources."
    )
    parser.add_argument("--team", help="Team to rebuild (or 'universe')")
    parser.add_argument("--all-teams", action="store_true", help="Rebuild every team with a commit_log")
    parser.add_argument(
        "--skip-semantic", action="store_true",
        help="Skip all embedding: the episodic re-embed, fact vectors, and logged "
             "semantic items. Skipped items stay in commit_log, so a later rebuild "
             "without this flag restores them; each one is named on stderr.")
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
