"""KWIM client - emit episodic events to the KWIM substrate.

A strict side-channel: emitting must never block the agent pipeline meaningfully
and must never raise into it. If KWIM is unconfigured, unreachable, or erroring,
the agents carry on exactly as before. Events are scheduled fire-and-forget.

Config:
  KWIM_BASE_URL  - e.g. http://kwim-service:8000
                   (unset means all emits are silent no-op)
  the team API key is read from <secrets-dir>/kwim-api-key (a mounted secret
  file; the directory defaults to /secrets, override with KWIM_SECRETS_DIR).
"""
import asyncio
import logging
import os

import httpx

from secret_reader import read_secret, secrets_dir

log = logging.getLogger(__name__)

KWIM_BASE_URL = os.environ.get("KWIM_BASE_URL", "").rstrip("/")


class KwimUnavailable(RuntimeError):
    """KWIM is unconfigured or unreachable, on a path that refuses to no-op.

    Raised only from the strict entry points below (`require_available`,
    `read_episodic(strict=True)`). The agent-facing emit paths stay fail-soft
    and never raise this: breaking an agent pipeline over a side-channel is
    worse than losing an event. For a job that exists solely to move KWIM
    data the tradeoff inverts - a silent success is the worse outcome, because
    it is indistinguishable from healthy and so can run dead for weeks.
    """

_api_key: str | None = None
_key_tried = False
_pending: set[asyncio.Task] = set()


def _key() -> str | None:
    global _api_key, _key_tried
    if _api_key is None and not _key_tried:
        _key_tried = True
        try:
            _api_key = read_secret("kwim-api-key")
        except Exception as exc:  # not mounted means KWIM stays a no-op
            log.warning("kwim: api key not available (%s); emits disabled", exc)
    return _api_key


def require_available() -> str:
    """Preflight for KWIM-only jobs: return the team key or raise.

    The strict counterpart to `_key()`. Call this at startup from anything whose
    whole purpose is KWIM work (the distiller), so a misconfigured deployment
    exits non-zero instead of reporting success for doing nothing.
    """
    if not KWIM_BASE_URL:
        raise KwimUnavailable("KWIM_BASE_URL is unset - there is nothing to talk to")
    key = _key()
    if not key:
        raise KwimUnavailable(
            f"KWIM api key not readable at {secrets_dir() / 'kwim-api-key'} - "
            "if the secret is mounted elsewhere, point KWIM_SECRETS_DIR at that "
            "directory (the default is /secrets)"
        )
    return key


async def _get(path: str, params: dict | None = None) -> dict | list | None:
    """Generic GET against KWIM. Returns parsed JSON or None on any failure."""
    key = _key()
    if not KWIM_BASE_URL or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{KWIM_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {key}"},
                params=params or {},
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        log.warning("kwim: %s failed (%s)", path, exc)
        return None


async def _post(path: str, body: dict | None = None) -> dict | None:
    """Generic POST against KWIM. Returns parsed JSON or None on any failure."""
    key = _key()
    if not KWIM_BASE_URL or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{KWIM_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=body or {},
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        log.warning("kwim: %s failed (%s)", path, exc)
        return None


async def _post_episodic(agent_id: str, session_id: str, event_type: str, event_data: dict) -> None:
    key = _key()
    if not KWIM_BASE_URL or not key:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{KWIM_BASE_URL}/v1/memory/episodic",
                headers={"Authorization": f"Bearer {key}"},
                json={"agent_id": agent_id, "session_id": session_id,
                      "event_type": event_type, "event_data": event_data or {}},
            )
    except Exception as exc:
        log.warning("kwim emit_episodic(%s) failed: %s", event_type, exc)


def emit_episodic(agent_id: str, session_id: str, event_type: str,
                  event_data: dict | None = None) -> None:
    """Schedule a fire-and-forget episodic emit. Non-blocking; never raises.

    Safe to call from any async context (the agents run under an event loop).
    The task is tracked so it isn't GC'd before completing.
    """
    try:
        task = asyncio.create_task(_post_episodic(agent_id, session_id, event_type, event_data or {}))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except RuntimeError:
        # No running loop (shouldn't happen in the agents) - skip silently.
        pass


# ---------------------------------------------------------------------------
# Read / write client methods
# ---------------------------------------------------------------------------

async def knowledge_query(
    fact_type: str | None = None,
    status: str = "current",
    about: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query current facts. Fail-soft by initializing empty []."""
    params: dict[str, object] = {"status_": status, "limit": limit}
    if fact_type:
        params["fact_type"] = fact_type
    if about:
        params["about"] = about
    result = await _get("/v1/knowledge/query", params)
    return result if isinstance(result, list) else []


async def knowledge_search(
    q: str,
    limit: int = 10,
    fact_type: str | None = None,
    about: list[str] | None = None,
) -> list[dict]:
    """Semantic search over governed facts. Fail-soft by initializing empty [].

    Use this when you do not already know the tag - `knowledge_query(about=[...])`
    is the exact-tag path, this one takes free text. Each result carries `score`
    (cosine distance, lower = closer) and is ordered nearest-first.
    """
    params: dict[str, object] = {"q": q, "limit": limit}
    if fact_type:
        params["fact_type"] = fact_type
    if about:
        params["about"] = about
    result = await _get("/v1/knowledge/search", params)
    return result if isinstance(result, list) else []


async def wisdom_rules(**situation) -> list[dict]:
    """Fetch approved wisdom rules matching a situation. Fail-soft with empty [].

    **situation is an open set of team-defined key/values, sent as
    situation.<k>=<v> query params and AND-matched server-side.
    """
    params: dict[str, object] = {}
    for k, v in situation.items():
        if v is not None:
            params[f"situation.{k}"] = v
    result = await _get("/v1/wisdom/rules", params)
    return result if isinstance(result, list) else []


async def wisdom_check(action: dict) -> dict:
    """Run a deterministic constraint check. Fail-soft with {'verdict': 'allow', '_degraded': True}."""
    result = await _post("/v1/wisdom/check", {"action": action})
    if isinstance(result, dict):
        return result
    return {"verdict": "allow", "_degraded": True}


async def memory_context(
    session_id: str,
    subject: str | None = None,
    **situation,
) -> dict:
    """Assemble working context for a turn. Fail-soft with empty bundle.

    `session_id` and `subject` are KWIM-interpreted (recent turns / knowledge
    `about` join). **situation is an open set of team-defined key/values,
    sent as situation.<k>=<v> params and matched against wisdom rules.
    """
    params: dict[str, object] = {"session_id": session_id}
    if subject:
        params["subject"] = subject
    for k, v in situation.items():
        if v is not None:
            params[f"situation.{k}"] = v
    result = await _get("/v1/memory/context", params)
    if isinstance(result, dict):
        return result
    _empty_coverage = {"covered": False, "n": 0}
    return {
        "recent": [], "knowledge": [], "wisdom": [],
        "coverage": {
            "knowledge": {**_empty_coverage, "queried": False},
            "wisdom": _empty_coverage,
            "recent": _empty_coverage,
        },
    }


async def memory_semantic(q: str | None = None, limit: int = 10, **meta) -> list[dict]:
    """Semantic recall over memory. **meta becomes meta.<k>=<v> query params.

    Two modes (matching the endpoint):
    - `q` given: KNN vector search, optionally metadata-filtered.
    - `q` omitted: metadata-only exact-match fetch (score=0.0)
    """
    params: dict[str, object] = {"limit": limit}
    if q is not None:
        params["q"] = q
    for k, v in meta.items():
        params[f"meta.{k}"] = v
    result = await _get("/v1/memory/semantic", params)
    return result if isinstance(result, list) else []


async def knowledge_propose(
    statement: str,
    fact_type: str,
    evidence: list[str] | None = None,
    supersedes: str | None = None,
    about: list[str] | None = None,
    source_kind: str = "agent_proposal",
    decay_class: str | None = None,
) -> dict | None:
    """Propose a new fact to the governance gate. Fail-soft with None."""
    body = {
        "statement": statement,
        "fact_type": fact_type,
        "evidence": evidence or [],
        "supersedes": supersedes,
        "about": about or [],
        "source_kind": source_kind,
    }
    if decay_class:
        body["decay_class"] = decay_class
    return await _post("/v1/knowledge/propose", body)


async def read_episodic(
    since_ts: str | None = None,
    since_id: str | None = None,
    limit: int = 500,
    event_type: str | None = None,
    agent_id: str | None = None,
    order: str = "asc",
    strict: bool = False,
) -> dict:
    """GET /v1/memory/episodic - windowed team read on the (occurred_at, id) cursor.

    order="desc" returns newest-first with the cursor as an exclusive upper bound -
    e.g. limit=1, order="desc" fetches the single latest matching event in O(1).

    Returns {events, next_cursor} or {events: [], next_cursor: None} on failure (fail-soft).

    strict=True raises KwimUnavailable on a failed read instead of returning the
    empty sentinel. Use it from jobs that branch on emptiness: fail-soft makes a
    broken read look exactly like "no new events", which reads as success.
    """
    params: dict[str, object] = {"limit": limit, "order": order}
    if since_ts is not None:
        params["since_ts"] = since_ts
    if since_id is not None:
        params["since_id"] = since_id
    if event_type:
        params["event_type"] = event_type
    if agent_id:
        params["agent_id"] = agent_id
    result = await _get("/v1/memory/episodic", params)
    if isinstance(result, dict) and "events" in result:
        return result
    if strict:
        raise KwimUnavailable(
            "read_episodic did not return a usable window - refusing to report "
            "'no new events', which would be indistinguishable from success"
        )
    return {"events": [], "next_cursor": None}


async def wisdom_propose(
    rule_type: str,
    *,
    situation: dict | None = None,
    approach: str | None = None,
    evidence: list[str] | None = None,
    source_kind: str = "agent_proposal",
    **constraint_fields,
) -> dict | None:
    """Propose a learned-rule candidate to the governance gate. Fail-soft with None.

    advisory: situation + approach + evidence. constraint fields (action_pattern,
    verdict, authority, severity, check_tier) are supported for completeness but
    the distiller does not auto-emit them.
    """
    body: dict[str, object] = {"rule_type": rule_type, "source_kind": source_kind}
    if rule_type == "advisory":
        body["situation"] = situation or {}
        body["approach"] = approach
        body["evidence"] = evidence or []
    else:
        body.update(constraint_fields)
    return await _post("/v1/wisdom/propose", body)
