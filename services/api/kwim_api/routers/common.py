"""Helpers shared by more than one router: fact enrichment, situation-param
parsing, and the best-effort policy the enrichment slots use.
"""
import logging

from fastapi import Request

from ..config import settings
from ..freshness import _to_dt, compute_freshness

log = logging.getLogger(__name__)

_FRESHNESS_SORT = {"fresh": 0, "aging": 1, "stale": 2}


async def best_effort(call, *, fallback, what: str):
    """Await `call()`, returning `fallback` if it raises.

    Enrichment slots degrade rather than fail the request; `what` names the slot
    in the warning so a degrade stays traceable.

    `call` is a callable, not an awaitable, so building the call is inside the
    guard: `State.embedder` is bound by the lifespan, and resolving it is one of
    the failures this absorbs.

    Not for knowledge/search - an empty result there is indistinguishable from
    "we know nothing", so it raises 503.
    """
    try:
        return await call()
    except Exception:
        log.warning("%s failed - degrading", what)
        return fallback


def _enrich_fact(r: dict) -> dict:
    """Add freshness + as_of to one fact row.

    `as_of` is the later of `created_at` and `last_verified_at` (if present),
    so a re-verified fact does not age while the pipeline is still vouching for it.
    """
    dc = r.get("decay_class") or "slow"
    created_dt = _to_dt(r.get("created_at"))
    verified_dt = _to_dt(r.get("last_verified_at"))
    as_of_dt = max((dt for dt in (created_dt, verified_dt) if dt is not None), default=None)
    as_of = as_of_dt.isoformat() if as_of_dt is not None else ""
    f = compute_freshness(as_of, dc, settings.halflife_slow_days, settings.halflife_fast_hours)
    return {**r, "decay_class": dc, "as_of": as_of, "freshness": f}


def _enrich_facts(rows: list[dict]) -> list[dict]:
    """Enrich every row and sort fresh-first - the ranking knowledge/query and the
    context bundle use. The sort is stable, so rows already ordered by relevance
    keep that order within a freshness band.

    knowledge/search deliberately does not use this: there, distance ordering is
    the answer, so it enriches per-row and leaves the ranking alone.
    """
    enriched = [_enrich_fact(r) for r in rows]
    enriched.sort(key=lambda x: _FRESHNESS_SORT.get(x["freshness"], 0))
    return enriched


def _situation_params(request: Request) -> dict[str, str]:
    """Collect ?situation.<key>=<val> params into an open situation dict."""
    situation: dict[str, str] = {}
    # When a handler is called directly (tests), `request` is the Ellipsis
    # sentinel, not a Request - same caveat as memory_context's `repos` param.
    qp = getattr(request, "query_params", None)
    if qp is not None:
        for key, val in qp.multi_items():
            if key.startswith("situation."):
                situation[key[10:]] = val  # strip "situation." prefix
    return situation
