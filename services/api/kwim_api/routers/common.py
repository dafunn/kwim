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
    """Await `call()`, returning `fallback` and logging `what` if it raises.
    `call` is a callable so that building the call is guarded too."""
    try:
        return await call()
    except Exception:
        log.warning("%s failed - degrading", what)
        return fallback


def _enrich_fact(r: dict) -> dict:
    """Add freshness and as_of (the later of created_at and last_verified_at)."""
    dc = r.get("decay_class") or "slow"
    created_dt = _to_dt(r.get("created_at"))
    verified_dt = _to_dt(r.get("last_verified_at"))
    as_of_dt = max((dt for dt in (created_dt, verified_dt) if dt is not None), default=None)
    as_of = as_of_dt.isoformat() if as_of_dt is not None else ""
    f = compute_freshness(as_of, dc, settings.halflife_slow_days, settings.halflife_fast_hours)
    return {**r, "decay_class": dc, "as_of": as_of, "freshness": f}


def _enrich_facts(rows: list[dict]) -> list[dict]:
    """Enrich every row and sort fresh-first, stably. Not used by knowledge/search,
    which keeps distance order."""
    enriched = [_enrich_fact(r) for r in rows]
    enriched.sort(key=lambda x: _FRESHNESS_SORT.get(x["freshness"], 0))
    return enriched


def _situation_params(request: Request) -> dict[str, str]:
    """Collect ?situation.<key>=<val> params into an open situation dict."""
    situation: dict[str, str] = {}
    # Called directly (tests), `request` is the Ellipsis default.
    qp = getattr(request, "query_params", None)
    if qp is not None:
        for key, val in qp.multi_items():
            if key.startswith("situation."):
                situation[key[10:]] = val  # strip "situation." prefix
    return situation
