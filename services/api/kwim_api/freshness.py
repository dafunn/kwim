"""Freshness for :Fact nodes: decay_class resolution and the fresh / aging /
stale bands. See docs/DESIGN.md, "Retrieval".
"""
from __future__ import annotations

from datetime import UTC, datetime

VALID_DECAY_CLASSES: frozenset[str] = frozenset({"permanent", "slow", "fast"})

# Fallback decay_class by fact_type, used when the proposer gives none.
_DECAY_CLASS_BY_TYPE: dict[str, str] = {
    "entity_attribute": "permanent",
    "trend":            "fast",
    "observation":      "slow",
    "product":          "slow",
}

_FRESHNESS_RANK: dict[str, int] = {"fresh": 0, "aging": 1, "stale": 2}


def resolve_decay_class(fact_type: str, override: str | None = None) -> str:
    """Resolve the decay class for a fact using the stated priority order."""
    if override and override in VALID_DECAY_CLASSES:
        return override
    return _DECAY_CLASS_BY_TYPE.get(fact_type, "slow")


def _to_dt(v) -> datetime | None:
    """Normalize epoch milliseconds or an ISO string to an aware datetime, or None."""
    if v is None:
        return None
    s = str(v)
    if not s or s == "None":
        return None
    try:
        if s.isdigit():
            return datetime.fromtimestamp(int(s) / 1000, tz=UTC)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def compute_freshness(
    as_of: str,
    decay_class: str,
    halflife_slow_days: float,
    halflife_fast_hours: float,
) -> str:
    """Return 'fresh', 'aging', or 'stale' given the fact's age and decay class."""
    if decay_class == "permanent":
        return "fresh"
    dt = _to_dt(as_of)
    if dt is None:
        return "stale"
    age_secs = (datetime.now(UTC) - dt).total_seconds()
    if decay_class == "fast":
        halflife_secs = halflife_fast_hours * 3600.0
    else:
        halflife_secs = halflife_slow_days * 86400.0
    ratio = age_secs / halflife_secs
    if ratio < 0.5:
        return "fresh"
    if ratio < 1.0:
        return "aging"
    return "stale"


def worst_freshness(items: list[str]) -> str:
    """Return the least-fresh value in a list ('stale' > 'aging' > 'fresh')."""
    if not items:
        return "fresh"
    return max(items, key=lambda x: _FRESHNESS_RANK.get(x, 0))
