"""Code-graph extractor entrypoint - the multi-stage extraction pipeline.

  1. structure   - discover files, compute content hashes
  2. extraction  - tree-sitter defs/imports/call-sites (parse.py)
  3. resolution  - confidence-scored CALLS edges (resolve.py)
  4. enrichment  - (Python-first: IMPORTS edges; effects deferred)
  5. flush       - write nodes/edges into kwim_<team>_code (FalkorStore)
  6. post-index  - Louvain communities -> MEMBER_OF

Run:
  python -m kwim_api.codegraph.extract --team <team> --repo <name> --path <checkout> [--no-embed]

Incremental: files whose xxh3 hash matches the graph are skipped (definitions),
but all files are parsed for the registry so cross-file calls still resolve.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from ..embedder import Embedder
from ..stores.falkor import FalkorStore
from . import incremental, parse, resolve
from .communities import detect_communities

log = logging.getLogger("codegraph.extract")


def _file_id(repo: str, rel_path: str) -> str:
    return f"{repo}:{rel_path}"


def _fn_id(repo: str, qn: str) -> str:
    return f"{repo}::{qn}"


async def extract_repo(
    store: FalkorStore, team: str, repo: str, repo_dir: str,
    embedder: Embedder | None = None,
) -> dict:
    commit = incremental.git_head(repo_dir)
    files = incremental.discover(repo_dir)
    prior_hashes = await store.code_files_by_hash(team, repo)

    # Read + parse every file (full registry needed for resolution).
    parsed: list[parse.ParsedFile] = []
    file_meta: dict[str, dict] = {}      # rel_path -> {hash, lang, changed}
    for rel, lang in files:
        try:
            with open(f"{repo_dir}/{rel}", "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        h = incremental.content_hash(data)
        file_meta[rel] = {"hash": h, "lang": lang, "changed": prior_hashes.get(rel) != h}
        try:
            parsed.append(parse.parse_python(rel, data))
        except Exception as exc:                       # never let one bad file abort the run
            log.warning("parse failed: %s/%s: %s", repo, rel, exc)

    # Resolution registry across the whole repo set.
    reg = resolve.build_registry(parsed)

    # Emit defs + capture edges for community detection.
    call_edges: list[tuple[str, str, float]] = []      # (caller_qn, callee_qn, confidence)
    n_defs = n_calls = 0
    for pf in parsed:
        meta = file_meta.get(pf.path, {})
        fid = _file_id(repo, pf.path)
        await store.materialize_code_file(
            team, file_id=fid, repo=repo, path=pf.path,
            lang=meta.get("lang", "python"), content_hash=meta.get("hash", ""), commit=commit,
        )
        # Definitions: only (re)write for changed files; embeddings are the costly bit.
        if meta.get("changed", True):
            await _write_defs(store, team, repo, fid, pf, embedder)
            n_defs += len(pf.defns)
        # IMPORTS edges (enrichment) - internal targets only for now.
        for imp in pf.imports:
            target_file = _internal_import_target(imp.target, parsed, repo)
            if target_file:
                await store.materialize_import_edge(team, file_id=fid, target=target_file, kind="internal")
            else:
                await store.materialize_import_edge(team, file_id=fid, target=imp.target, kind="external")

        # Resolution + CALLS edges (always - cross-file callers may have changed).
        for caller_qn, _callee, res in resolve.resolve_file(reg, pf):
            await store.materialize_call_edge(
                team, caller_id=_fn_id(repo, caller_qn), callee_id=_fn_id(repo, res.qn),
                confidence=res.confidence, strategy=res.strategy, candidates=res.candidates,
            )
            call_edges.append((caller_qn, res.qn, res.confidence))
            n_calls += 1

    # Prune: drop any File (+ contained defs) no longer discovered - deleted files
    # or newly-excluded paths (.cgignore). MERGE never removes; this does, so the
    # graph and the distiller stop seeing stale nodes (e.g. excluded vendored trees).
    keep_paths = [rel for rel, _ in files]
    pruned = await store.prune_repo_files(team, repo=repo, keep_paths=keep_paths)

    # Louvain communities -> MEMBER_OF.
    comms = detect_communities(call_edges)
    for qn, cid in comms.items():
        await store.set_member_of(team, fn_id=_fn_id(repo, qn), community=cid)

    summary = {"repo": repo, "commit": commit, "files": len(parsed),
               "definitions": n_defs, "calls": n_calls, "communities": len(set(comms.values())),
               "pruned_files": pruned}
    log.info("codegraph extract done: %s", summary)
    return summary


async def _write_defs(
    store: FalkorStore, team: str, repo: str, fid: str, pf: parse.ParsedFile,
    embedder: Embedder | None,
) -> None:
    funcs = [d for d in pf.defns if d.kind in ("function", "method")]
    embeddings: list[list[float]] | None = None
    if embedder is not None and funcs:
        texts = [f"{d.signature} {d.summary}".strip() for d in funcs]
        try:
            embeddings = await embedder.embed(texts)
        except Exception as exc:
            log.warning("embed failed for %s/%s: %s", repo, pf.path, exc)
            embeddings = None
    fi = 0
    for d in pf.defns:
        if d.kind == "class":
            await store.materialize_code_class(
                team, cls_id=_fn_id(repo, d.qn), repo=repo, path=pf.path,
                name=d.name, methods=d.methods, file_id=fid,
            )
        else:
            emb = embeddings[fi] if embeddings is not None and fi < len(embeddings) else None
            await store.materialize_code_function(
                team, fn_id=_fn_id(repo, d.qn), repo=repo, path=pf.path, name=d.name,
                signature=d.signature, summary=d.summary,
                start_line=d.start_line, end_line=d.end_line, file_id=fid, embedding=emb,
            )
            fi += 1


def _internal_import_target(target: str, parsed: list[parse.ParsedFile], repo: str) -> str | None:
    """If `target` (a module path or module.symbol) maps to an indexed file in this
    repo, return that file_id; else None (external)."""
    module_qns = {pf.module_qn: pf.path for pf in parsed}
    # exact module match
    if target in module_qns:
        return _file_id(repo, module_qns[target])
    # module.symbol -> drop the trailing symbol
    head = target.rsplit(".", 1)[0]
    if head in module_qns:
        return _file_id(repo, module_qns[head])
    return None


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store = FalkorStore()
    await store.connect()
    embedder = None if args.no_embed else Embedder()
    try:
        await extract_repo(store, args.team, args.repo, args.path, embedder)
    finally:
        if embedder is not None:
            await embedder.close()
        await store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kwim_api.codegraph.extract", description="KWIM code-graph extractor")
    ap.add_argument("--team", required=True, help="tenant team (graph kwim_<team>_code)")
    ap.add_argument("--repo", required=True, help="repo name (node property + id prefix)")
    ap.add_argument("--path", required=True, help="path to the checked-out repo")
    ap.add_argument("--no-embed", action="store_true", help="skip function embeddings (no embedder)")
    args = ap.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
