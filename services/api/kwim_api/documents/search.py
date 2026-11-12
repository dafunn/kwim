"""Document search: vector and full-text search over chunks, merged by
reciprocal rank fusion, optionally rescored by a reranker, then expanded to the
sections that contain the hits.

Searches the team's docs graph and the universe docs graph. A section larger
than `documents.search.max_section_tokens` returns the hit chunks and their
neighbours instead of the whole section.
"""
import logging
import re
from typing import Any

from ..config import settings
from .chunk import estimate_tokens

log = logging.getLogger(__name__)

UNIVERSE = "universe"

# Terms for the full-text query. RediSearch syntax characters are dropped, so a
# question cannot produce a query syntax error.
_TERM = re.compile(r"[A-Za-z0-9_]+")
_MAX_TERMS = 24


def fulltext_query(q: str) -> str | None:
    """OR of the query's terms, or None when it has none."""
    terms: list[str] = []
    for term in _TERM.findall(q):
        if term.lower() not in (t.lower() for t in terms):
            terms.append(term)
    return "|".join(terms[:_MAX_TERMS]) or None


def rrf(*rankings: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Fuse ranked chunk lists by reciprocal rank: score = sum of 1 / (k + rank)."""
    fused: dict[tuple[str, str], dict[str, Any]] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            key = (chunk["team"], chunk["id"])
            entry = fused.setdefault(key, {**chunk, "rrf": 0.0})
            entry["rrf"] += 1.0 / (k + rank)
    return sorted(fused.values(), key=lambda c: c["rrf"], reverse=True)


async def search(falkor: Any, embedder: Any, team: str, q: str, *, limit: int,
                 document_ids: list[str] | None = None, tag: str | None = None,
                 reranker: Any = None) -> dict[str, Any]:
    """Ranked sections for `q`, best first. Returns {"results": [...], "coverage": {...}}."""
    fused, coverage = await candidates(falkor, embedder, team, q,
                                       document_ids=document_ids, tag=tag)
    ranked, coverage["reranked"] = await rerank(reranker, q, fused)
    return {"results": await expand(falkor, ranked, limit), "coverage": coverage}


async def rerank(reranker: Any, q: str,
                 fused: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Rescore the top fused candidates with the reranker. The rest keep their
    fused order after them. Without a reranker, or when it fails, `fused` is
    returned unchanged."""
    if reranker is None or not fused:
        return fused, False
    n = settings.documents_reranker_candidates
    head, tail = fused[:n], fused[n:]
    try:
        scores = await reranker.scores(
            q, [f"{c['heading_path']}:\n{c['text']}" for c in head])
    except Exception as exc:
        log.warning("document search: reranker failed, keeping fused order: %s", exc)
        return fused, False
    order = sorted(range(len(head)), key=lambda i: scores[i], reverse=True)
    return [{**head[i], "rerank": scores[i]} for i in order] + tail, True


async def candidates(falkor: Any, embedder: Any, team: str, q: str, *,
                     document_ids: list[str] | None = None,
                     tag: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Chunks from vector and full-text search, fused and ranked, with coverage."""
    teams = [team] if team == UNIVERSE else [team, UNIVERSE]
    coverage: dict[str, Any] = {"vector": False, "fulltext": False, "reason": None}

    scoped: dict[str, list[str] | None] = {}
    for t in teams:
        ids = document_ids
        if tag is not None:
            tagged = [d["id"] for d in await falkor.docs_documents(t, tag=tag)]
            ids = [i for i in tagged if document_ids is None or i in document_ids]
        scoped[t] = ids
    if all(ids == [] for ids in scoped.values()):
        return [], {**coverage, "reason": "no matching documents"}

    qvec = None
    if embedder is not None:
        try:
            qvec = (await embedder.embed([q]))[0]
        except Exception as exc:
            log.warning("document search: embedding the query failed: %s", exc)
            coverage["reason"] = "embedder unavailable; full-text only"
    else:
        coverage["reason"] = "no document embedder configured; full-text only"

    ft = fulltext_query(q)
    vector_hits: list[dict[str, Any]] = []
    text_hits: list[dict[str, Any]] = []
    # A team document whose universe copy is searchable is found through the copy.
    in_universe = ({d["id"] for d in await falkor.docs_documents(UNIVERSE)}
                   if UNIVERSE in teams and team != UNIVERSE else set())
    for t in teams:
        if scoped[t] == []:
            continue
        hidden = set() if t == UNIVERSE else {
            d["id"] for d in await falkor.docs_documents(t)
            if d.get("promoted_to") and d["promoted_to"] in in_universe}
        if qvec is not None:
            rows = await falkor.docs_vector_search(
                t, qvec, settings.documents_search_vector_k, scoped[t])
            vector_hits += [{**r, "team": t} for r in rows if r["document_id"] not in hidden]
        if ft is not None:
            rows = await falkor.docs_fulltext_search(
                t, ft, settings.documents_search_fulltext_k, scoped[t])
            text_hits += [{**r, "team": t} for r in rows if r["document_id"] not in hidden]
    vector_hits.sort(key=lambda c: c["score"])
    text_hits.sort(key=lambda c: c["score"], reverse=True)
    coverage["vector"] = bool(vector_hits)
    coverage["fulltext"] = bool(text_hits)

    return rrf(vector_hits, text_hits, k=settings.documents_search_rrf_k), coverage


async def expand(falkor: Any, fused: list[dict[str, Any]], limit: int) -> list[dict]:
    """Group hits by section in rank order and fetch each section."""
    order: list[tuple[str, str]] = []
    hits: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for chunk in fused:
        key = (chunk["team"], chunk["section_id"])
        if key not in hits:
            if len(order) >= limit:
                continue
            order.append(key)
            hits[key] = []
        hits[key].append(chunk)

    results = []
    for team, section_id in order:
        section = await falkor.docs_section(team, section_id)
        if section is None:
            continue
        matched = hits[(team, section_id)]
        results.append(_result(team, section, matched))
    return results


def _result(team: str, section: dict[str, Any], matched: list[dict[str, Any]]) -> dict:
    chunks = section["chunks"]
    matched_ids = {c["id"] for c in matched}
    total = sum(estimate_tokens(c["text"]) for c in chunks)
    complete = total <= settings.documents_search_max_section_tokens
    if not complete:
        keep: set[int] = set()
        for i, c in enumerate(chunks):
            if c["id"] in matched_ids:
                keep.update({i - 1, i, i + 1})
        chunks = [c for i, c in enumerate(chunks) if i in keep]
    pages = [p for c in chunks for p in (c["page_start"], c["page_end"]) if p is not None]
    doc_id = section["document_id"]
    return {
        "document_id": doc_id,
        "document_title": section["document_title"],
        "revision_number": section["revision_number"],
        "scope": "universe" if team == UNIVERSE else "team",
        "section_id": section["id"].split(":", 1)[1],
        "heading": section["heading"],
        "path": section["path"],
        "page_start": min(pages) if pages else section["page_start"],
        "page_end": max(pages) if pages else section["page_end"],
        "complete_section": complete,
        "text": "\n\n".join(c["text"] for c in chunks),
        "matched_chunks": [c["id"].split(":", 1)[1] for c in matched],
        "score": round(sum(c["rrf"] for c in matched), 6),
        "figures": [{"figure_id": f["figure_id"], "caption": f["caption"], "page": f["page"],
                     "url": (f"/v1/documents/{doc_id}/figures/{f['figure_id']}"
                             if f["object_key"] else None)}
                    for f in section["figures"]],
    }
