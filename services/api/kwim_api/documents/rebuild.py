"""Rebuild a team's docs graph (kwim_<team>_docs) from Postgres: re-chunk,
re-embed and re-project every ready document's current revision from its stored
extraction. Reads Postgres only; the original files are not used.

The documents are projected into `kwim_<team>_docs_rebuild`, which is swapped in
with GRAPH.COPY; documents uploaded, revised, promoted, retracted or forgotten in
the meantime are then caught up in the live graph. The swap backs the live graph
up to `kwim_<team>_docs_prerebuild` and restores it if the copy fails. If any
document fails to project, nothing is swapped. Without GRAPH.COPY the live graph
is rebuilt in place, and the team's document search is empty until it finishes.

    python -m kwim_api.documents.rebuild --team <team> [--yes]
    python -m kwim_api.documents.rebuild --all-teams [--yes]
"""
import argparse
import asyncio
import sys
from typing import Any

from ..stores.falkor import FalkorStore
from ..stores.falkor_docs import _docs_graph_name, docs_graph_lock
from ..stores.postgres import PostgresStore
from .ingest import document_embedder
from .project import project_current
from .search import UNIVERSE

_CATCH_UP_ROUNDS = 10


class SwapUnsupported(Exception):
    """This FalkorDB has no GRAPH.COPY."""


async def _delete_graph(conn, name: str) -> None:
    """GRAPH.DELETE that treats a missing graph as done."""
    try:
        await conn.execute_command("GRAPH.DELETE", name)
    except Exception as exc:
        msg = str(exc).lower()
        if not ("empty key" in msg or "not exist" in msg or "unknown graph" in msg):
            raise


async def _swap_graphs(falkor: FalkorStore, team: str) -> bool:
    """Make the temp graph the live one, under the live docs graph's lock: copy
    live to a backup, delete live, copy temp onto its name. If that copy fails
    the backup is copied back. Raises SwapUnsupported without GRAPH.COPY."""
    live = _docs_graph_name(team)
    temp, backup = f"{live}_rebuild", f"{live}_prerebuild"
    conn = falkor._db.connection
    async with docs_graph_lock(team):
        await _delete_graph(conn, backup)
        try:
            await conn.execute_command("GRAPH.COPY", live, backup)
        except Exception as exc:
            if "unknown command" in str(exc).lower():
                raise SwapUnsupported(str(exc)) from exc
            if "empty key" not in str(exc).lower():   # no live graph yet: nothing to back up
                print(f"  could not back up {live} ({exc}); live graph untouched",
                      file=sys.stderr)
                return False
        try:
            await _delete_graph(conn, live)
            await conn.execute_command("GRAPH.COPY", temp, live)
        except Exception as exc:
            print(f"  GRAPH.COPY swap failed ({exc}); restoring {live} from {backup}",
                  file=sys.stderr)
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


async def _project_all(pg: Any, falkor: Any, embedder: Any, team: str,
                       document_ids: list[str], graph_name: str | None) -> dict:
    scope = "universe" if team == UNIVERSE else "team"
    done, failed, chunks = 0, [], 0
    for document_id in document_ids:
        try:
            result = await project_current(pg, falkor, embedder, team, document_id,
                                           scope=scope, graph_name=graph_name)
            done += 1
            chunks += result["chunks"]
        except Exception as exc:
            failed.append({"document_id": document_id, "error": str(exc)})
    return {"documents": done, "chunks": chunks, "failed": failed}


async def _catch_up(pg: Any, falkor: Any, embedder: Any, team: str,
                    projected: dict[str, tuple[str, str | None]]) -> dict:
    """Bring the live graph in line with documents changed since `projected`
    was read: project the new or changed ones, remove the ones no longer ready."""
    caught_up, failed = 0, []
    for _ in range(_CATCH_UP_ROUNDS):
        now = await pg.list_projectable(team)
        changed = [d for d, state in now.items() if projected.get(d) != state]
        gone = [d for d in projected if d not in now]
        if not changed and not gone:
            break
        for document_id in gone:
            await falkor.remove_document_projection(team, document_id)
        result = await _project_all(pg, falkor, embedder, team, changed, None)
        caught_up += len(gone) + result["documents"]
        failed += result["failed"]
        projected = now
    else:
        print("  WARNING: documents still changing after "
              f"{_CATCH_UP_ROUNDS} catch-up rounds", file=sys.stderr)
    return {"caught_up": caught_up, "failed": failed}


async def rebuild_team(pg: PostgresStore, falkor: FalkorStore, embedder, team: str) -> dict:
    """Rebuild one team's docs graph. `swapped` is False when the live graph was
    left as it was."""
    live = _docs_graph_name(team)
    temp = f"{live}_rebuild"
    conn = falkor._db.connection
    # A temp graph left by an earlier rebuild that did not finish.
    await _delete_graph(conn, temp)
    falkor._inited.discard(temp)

    projected = await pg.list_projectable(team)
    result = await _project_all(pg, falkor, embedder, team, list(projected), temp)
    out = {"team": team, **result, "swapped": False, "caught_up": 0}
    if result["failed"]:
        await _delete_graph(conn, temp)
        return out

    try:
        swapped = await _swap_graphs(falkor, team)
    except SwapUnsupported as exc:
        print(f"  GRAPH.COPY unavailable ({exc}); rebuilding {live} in place",
              file=sys.stderr)
        await _delete_graph(conn, temp)
        async with docs_graph_lock(team):
            await _delete_graph(conn, live)
            falkor._inited.discard(live)
        projected = await pg.list_projectable(team)
        result = await _project_all(pg, falkor, embedder, team, list(projected), None)
        out.update(result)
        swapped = True
    if not swapped:
        return out
    out["swapped"] = True
    catch_up = await _catch_up(pg, falkor, embedder, team, projected)
    out["caught_up"] = catch_up["caught_up"]
    out["failed"] = out["failed"] + catch_up["failed"]
    return out


async def _amain(args: argparse.Namespace) -> int:
    pg, falkor = PostgresStore(), FalkorStore()
    await pg.connect()
    await falkor.connect()
    embedder = document_embedder()
    try:
        teams = await pg.list_team_schemas() if args.all_teams else [args.team]
        if not args.yes:
            answer = input(f"Rebuild the docs graph for {', '.join(teams)}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        if embedder is None:
            print("KWIM_DOCS_EMBEDDER_URL is unset: chunks will have no vectors",
                  file=sys.stderr)
        status = 0
        for team in teams:
            result = await rebuild_team(pg, falkor, embedder, team)
            state = "swapped in" if result["swapped"] else "NOT swapped; live graph unchanged"
            print(f"[{team}] {result['documents']} document(s), {result['chunks']} chunk(s), "
                  f"{result['caught_up']} caught up; {state}")
            for f in result["failed"]:
                print(f"  FAILED {f['document_id']}: {f['error']}", file=sys.stderr)
            if result["failed"] or not result["swapped"]:
                status = 1
        return status
    finally:
        if embedder is not None:
            await embedder.close()
        await falkor.close()
        await pg.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rebuild docs graphs from Postgres.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--team")
    g.add_argument("--all-teams", action="store_true")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return asyncio.run(_amain(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
