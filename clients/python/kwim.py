"""KWIM client - emit episodic events to the KWIM substrate, read memory,
knowledge, wisdom, documents, and skills, and propose knowledge, wisdom, and
skills.

Agent-facing calls never raise: without configuration, or with KWIM unreachable,
they return empty defaults, and emits are fire-and-forget. See docs/DESIGN.md,
"The client and the distiller".

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

    Raised only by the strict entry points, `require_available` and
    `read_episodic(strict=True)`.
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

    For jobs whose whole purpose is KWIM work, such as the distiller.
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


async def _get(path: str, params: dict | None = None,
               timeout: float = 10.0) -> dict | list | None:
    """Generic GET against KWIM. Returns parsed JSON or None on any failure."""
    key = _key()
    if not KWIM_BASE_URL or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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


async def _get_bytes(path: str) -> bytes | None:
    """GET a binary resource from KWIM. Returns the body or None on any failure."""
    key = _key()
    if not KWIM_BASE_URL or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{KWIM_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {key}"},
            )
            r.raise_for_status()
            return r.content
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

    Call from a running event loop. The task is kept referenced until it completes.
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

    Free-text counterpart to `knowledge_query(about=[...])`. Results are ordered
    nearest first; `score` is a cosine distance.
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

    Each **situation item becomes a situation.<k>=<v> param; all must match.
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

    `session_id` selects recent events and `subject` the knowledge; each
    **situation item becomes a situation.<k>=<v> param for the wisdom rules.
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

    order="desc" returns newest first, with the cursor as an exclusive upper bound.

    Returns {events, next_cursor}, or {events: [], next_cursor: None} on failure.
    strict=True raises KwimUnavailable on a failed read instead.
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

    advisory: situation, approach, and evidence. constraint: action_pattern,
    verdict, authority, severity, and check_tier.
    """
    body: dict[str, object] = {"rule_type": rule_type, "source_kind": source_kind}
    if rule_type == "advisory":
        body["situation"] = situation or {}
        body["approach"] = approach
        body["evidence"] = evidence or []
    else:
        body.update(constraint_fields)
    return await _post("/v1/wisdom/propose", body)


# ---------------------------------------------------------------------------
# Documents: manuals and references uploaded by operators, searched by section
# ---------------------------------------------------------------------------

# Search embeds the query and may rerank up to 40 candidates on CPU.
_DOCUMENTS_SEARCH_TIMEOUT = 30.0


async def documents_list(tag: str | None = None) -> list[dict]:
    """The team's and the universe's searchable documents. Fail-soft with [].

    Each item has id, title, tags, revision_number, mime_type, page_count,
    scope ("team" or "universe"), and promoted_to.
    """
    params: dict[str, object] = {}
    if tag:
        params["tag"] = tag
    result = await _get("/v1/documents", params)
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return result["items"]
    return []


async def documents_search(
    q: str,
    limit: int = 5,
    document_ids: list[str] | None = None,
    tag: str | None = None,
) -> dict:
    """Sections of the team's and the universe's documents that answer `q`,
    best first. Fail-soft with {"results": [], "coverage": {}, "_degraded": True}.

    Each result carries its citation (document_id, document_title,
    revision_number, section_id, heading, path, page_start, page_end), the
    section `text` (the whole section when `complete_section`, else the matched
    chunks and their neighbours), and `figures`, each with a `url` for
    `documents_figure`. `coverage` says whether vector search, full-text search,
    and reranking were used.
    """
    params: dict[str, object] = {"q": q, "limit": limit}
    if document_ids:
        params["document"] = document_ids
    if tag:
        params["tag"] = tag
    result = await _get("/v1/documents/search", params, timeout=_DOCUMENTS_SEARCH_TIMEOUT)
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        return result
    return {"results": [], "coverage": {}, "_degraded": True}


async def documents_section(document_id: str, section_id: str) -> dict | None:
    """One whole section, by the ids a search result gives. Fail-soft with None.

    Use it to read a section in full when a search result is not
    `complete_section`.
    """
    result = await _get(f"/v1/documents/{document_id}/sections/{section_id}")
    return result if isinstance(result, dict) else None


async def documents_figure(document_id: str, figure_id: str) -> bytes | None:
    """A figure's PNG image. Fail-soft with None."""
    return await _get_bytes(f"/v1/documents/{document_id}/figures/{figure_id}")


async def documents_promote(document_id: str) -> dict | None:
    """Copy one of the team's documents into the universe. Requires the key's
    `promote` capability. Fail-soft with None."""
    return await _post(f"/v1/documents/{document_id}/promote")


# ---------------------------------------------------------------------------
# Skills: task instructions to search for on each turn and load by name
# ---------------------------------------------------------------------------

# Search embeds the request; allow for a cold embedder.
_SKILLS_SEARCH_TIMEOUT = 15.0


def _skills_unavailable(reason: str) -> dict:
    return {"results": [], "coverage": {"available": False, "reason": reason}}


async def skills_search(q: str, limit: int = 5, agent: str | None = None,
                        tag: str | None = None) -> dict:
    """Skills that match the turn's request `q`, best first: names and
    descriptions. `agent` limits them to skills offered to that persona.

    Never raises. When KWIM is unconfigured or unreachable the result is
    {"results": [], "coverage": {"available": False, "reason": ...}}, so "no
    skills match" (available, no results) and "skills unavailable" are told
    apart; show the reason to the operator.
    """
    key = _key()
    if not KWIM_BASE_URL or not key:
        return _skills_unavailable("KWIM is not configured (KWIM_BASE_URL or the api key)")
    params: dict[str, object] = {"q": q, "limit": limit}
    if agent:
        params["agent"] = agent
    if tag:
        params["tag"] = tag
    try:
        async with httpx.AsyncClient(timeout=_SKILLS_SEARCH_TIMEOUT) as client:
            r = await client.get(
                f"{KWIM_BASE_URL}/v1/skills/search",
                headers={"Authorization": f"Bearer {key}"},
                params=params,
            )
            r.raise_for_status()
            result = r.json()
    except Exception as exc:
        log.warning("kwim: /v1/skills/search failed (%s)", exc)
        return _skills_unavailable(f"KWIM is unreachable: {type(exc).__name__}: {exc}")
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        return result
    return _skills_unavailable("KWIM returned an unexpected skills search response")


async def skill_get(name: str) -> dict | None:
    """The skill to follow: body, refs, version, and `pending_revision` when a
    revision awaits review. Fail-soft with None."""
    result = await _get(f"/v1/skills/{name}")
    return result if isinstance(result, dict) else None


async def skills_list(agent: str | None = None, tag: str | None = None) -> list[dict]:
    """Every skill the team can load, by name. Fail-soft with []."""
    params: dict[str, object] = {}
    if agent:
        params["agent"] = agent
    if tag:
        params["tag"] = tag
    result = await _get("/v1/skills", params)
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return result["items"]
    return []


async def skill_propose(text: str, supersedes: str | None = None,
                        scope: str = "team") -> dict | None:
    """Propose a new skill (the whole SKILL.md text), or a revision of one with
    `supersedes` (`scope="universe"` for a universe skill). It waits for human
    review. Fail-soft with None."""
    body: dict[str, object] = {"text": text, "scope": scope}
    if supersedes:
        body["supersedes"] = supersedes
    return await _post("/v1/skills/propose", body)


async def skill_promote(name: str) -> dict | None:
    """Copy one of the team's skills into the universe. Requires the key's
    `promote` capability. Fail-soft with None."""
    return await _post(f"/v1/skills/{name}/promote")
