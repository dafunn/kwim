"""Post-index: Louvain community detection over the CALLS graph.

Cohesive call-clusters become module-like communities (MEMBER_OF edges), backing
get_architecture's "what are the modules" view.
Uses networkx's built-in Louvain (worker-only dependency, not in the request path).
"""
from __future__ import annotations

import networkx as nx

from ..config import settings


def detect_communities(
    call_edges: list[tuple[str, str, float]], min_confidence: float | None = None,
) -> dict[str, int]:
    """call_edges: (caller_qn, callee_qn, confidence). Returns {function_qn:
    community_id}. Low-confidence edges are excluded so fuzzy guesses don't merge
    unrelated clusters. `min_confidence` defaults to settings.cg_community_min_confidence."""
    if min_confidence is None:
        min_confidence = settings.cg_community_min_confidence
    g = nx.Graph()
    for caller, callee, conf in call_edges:
        if conf < min_confidence:
            continue
        # weight by confidence; accumulate if the pair recurs
        if g.has_edge(caller, callee):
            g[caller][callee]["weight"] += conf
        else:
            g.add_edge(caller, callee, weight=conf)
    if g.number_of_nodes() == 0:
        return {}
    communities = nx.community.louvain_communities(g, weight="weight", seed=42)
    out: dict[str, int] = {}
    for cid, members in enumerate(communities):
        for qn in members:
            out[qn] = cid
    return out
