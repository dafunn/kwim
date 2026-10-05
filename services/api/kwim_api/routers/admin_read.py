"""Cross-team read API for the admin console. Every route requires an operator
(`CurrentOperator`). Team content comes from `State.pg` and `State.falkor`; keys,
operators, audit and the team registry from `State.admin`.
"""
import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..admin_auth import AdminContext, CurrentOperator
from ..gate import summarize_proposal
from ..models import CodeArchitecture, CodeChange, CodeFunction, Fact, FactProvenance
from ..pagination import decode_cursor
from ..pagination import next_cursor as _next_cursor
from ..runtime import State
from ..stores.postgres import _IDENT
from .common import _enrich_fact, best_effort

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Team-path validation - the only guard on the Cypher paths, which
# never go through PostgresStore._schema.
# ---------------------------------------------------------------------------

async def _team_path(team: str, _op: AdminContext = CurrentOperator) -> str:
    """Validate the team in the path after authenticating the operator (`_op`),
    so an unauthenticated caller gets 401 before any 404 or 422."""
    if not _IDENT.match(team):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="invalid team identifier")
    schemas = await State.pg.list_team_schemas()
    if team in schemas or await State.admin.get_team(team) is not None:
        return team
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"team {team!r} not found")


TeamPath = Depends(_team_path)


async def _team_counts(team: str) -> dict[str, int]:
    """facts/rules/semantic/episodic/pending counts; a failing count is 0."""
    facts = await best_effort(lambda: State.falkor.count_facts_admin(team), fallback=0,
                              what=f"admin team counts facts (team={team!r})")
    rules = await best_effort(lambda: State.falkor.count_rules_admin(team), fallback=0,
                              what=f"admin team counts rules (team={team!r})")
    semantic = await best_effort(lambda: State.falkor.count_semantic(team), fallback=0,
                                 what=f"admin team counts semantic (team={team!r})")
    episodic = await best_effort(lambda: State.pg.count_episodic(team), fallback=0,
                                 what=f"admin team counts episodic (team={team!r})")
    pending = await best_effort(lambda: State.pg.pending_stats(team), fallback={"count": 0},
                                what=f"admin team counts pending (team={team!r})")
    return {"facts": facts, "rules": rules, "semantic": semantic, "episodic": episodic,
            "pending": pending["count"]}


def _rule_out(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in
            ("id", "rule_type", "situation", "approach", "evidence_count", "status",
             "scope", "action_pattern", "verdict", "authority", "severity", "check_tier")}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@router.get("/teams")
async def admin_teams(count: bool = False, _op: AdminContext = CurrentOperator):
    """Console records reconciled with the schemas that exist, each flagged with
    has_schema and has_console_record."""
    schemas = set(await State.pg.list_team_schemas())
    console_rows = {r["team"]: r for r in await State.admin.list_teams()}
    names = sorted(schemas | set(console_rows))

    items = []
    for name in names:
        row = console_rows.get(name)
        items.append({
            "team": name,
            "display_name": row["display_name"] if row else None,
            "status": row["status"] if row else None,
            "created_at": row["created_at"] if row else None,
            "has_schema": name in schemas,
            "has_console_record": row is not None,
            "counts": await _team_counts(name) if count else None,
        })
    return {"items": items}


@router.get("/teams/{team}")
async def admin_team_detail(team: str = TeamPath, _op: AdminContext = CurrentOperator):
    console_row = await State.admin.get_team(team)
    schemas = await State.pg.list_team_schemas()
    has_schema = team in schemas
    indexed_repos = sorted(await State.falkor.code_indexed_repos(team))
    latest = await State.pg.read_commit_log(team, order="desc", limit=1) if has_schema else []
    return {
        "team": team,
        "display_name": console_row["display_name"] if console_row else None,
        "status": console_row["status"] if console_row else None,
        "created_at": console_row["created_at"] if console_row else None,
        "has_schema": has_schema,
        "has_console_record": console_row is not None,
        "counts": await _team_counts(team),
        "graph": f"kwim_{team}",
        "code_graph": f"kwim_{team}_code",
        "indexed_repos": indexed_repos,
        "latest_commit_seq": latest[0]["seq"] if latest else None,
        "latest_committed_at": latest[0]["committed_at"].isoformat() if latest else None,
    }


# ---------------------------------------------------------------------------
# Knowledge (facts)
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/facts")
async def admin_facts(
    team: str = TeamPath, status_: str = Query("any", alias="status"),
    fact_type: str | None = None, about: list[str] | None = Query(None),
    source_kind: str | None = None, q: str | None = None,
    cursor: str | None = None, limit: int = 50, count: bool = False,
    _op: AdminContext = CurrentOperator,
):
    status_filter = None if status_ == "any" else status_
    decoded = decode_cursor(cursor) if cursor else None
    rows = await State.falkor.query_facts_admin(
        team, status=status_filter, fact_type=fact_type, source_kind=source_kind,
        about=about, q=q, cursor=decoded, limit=limit)
    items = [Fact(**{k: v for k, v in _enrich_fact(r).items() if k != "commit_seq"}).model_dump()
             for r in rows]
    next_cursor = _next_cursor(
        rows, limit, lambda r: {"seq": r["commit_seq"] or 0, "id": r["id"]})
    total = (await State.falkor.count_facts_admin(
        team, status=status_filter, fact_type=fact_type, source_kind=source_kind,
        about=about, q=q) if count else None)
    return {"items": items, "next_cursor": next_cursor, "total": total}


@router.get("/teams/{team}/facts/{fact_id}")
async def admin_fact_detail(fact_id: str, team: str = TeamPath,
                            _op: AdminContext = CurrentOperator):
    row = await State.falkor.get_fact_provenance(team, fact_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"fact {fact_id} not found")
    audit_chain = await State.falkor.audit_fact(team, fact_id)
    commit_rows = await State.pg.read_commit_log(team, filters={"object_id": fact_id}, limit=1000)

    enriched = _enrich_fact(row)
    fact = Fact(id=row["id"], statement=row["statement"], fact_type=row["fact_type"],
               status=row["status"], created_at=row["created_at"],
               source_kind=row.get("source_kind"), last_verified_at=row.get("last_verified_at"),
               as_of=enriched["as_of"], freshness=enriched["freshness"])
    provenance = FactProvenance(proposed_by=row["proposed_by"], supported_by=row["supported_by"],
                                supersedes=row["supersedes"])
    return {
        "fact": fact.model_dump(), "provenance": provenance.model_dump(),
        "audit_chain": audit_chain,
        "commit_rows": [
            {**r, "committed_at": r["committed_at"].isoformat()} for r in commit_rows
        ],
    }


# ---------------------------------------------------------------------------
# Wisdom (rules)
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/rules")
async def admin_rules(
    team: str = TeamPath, status_: str = Query("any", alias="status"),
    rule_type: str | None = None, scope: str | None = None,
    cursor: str | None = None, limit: int = 50, count: bool = False,
    _op: AdminContext = CurrentOperator,
):
    status_filter = None if status_ == "any" else status_
    decoded = decode_cursor(cursor) if cursor else None
    rows = await State.falkor.query_rules_admin(
        team, status=status_filter, rule_type=rule_type, scope=scope, cursor=decoded, limit=limit)
    items = [_rule_out(r) for r in rows]
    next_cursor = _next_cursor(
        rows, limit, lambda r: {"seq": r["commit_seq"] or 0, "id": r["id"]})
    total = (await State.falkor.count_rules_admin(
        team, status=status_filter, rule_type=rule_type, scope=scope) if count else None)
    return {"items": items, "next_cursor": next_cursor, "total": total}


@router.get("/teams/{team}/rules/{rule_id}")
async def admin_rule_detail(rule_id: str, team: str = TeamPath,
                            _op: AdminContext = CurrentOperator):
    row = await State.falkor.get_rule_provenance(team, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found")
    commit_rows = await State.pg.read_commit_log(team, filters={"object_id": rule_id}, limit=1000)
    return {
        "rule": _rule_out(row),
        "provenance": {
            "proposed_by": row["proposed_by"], "learned_from": row["learned_from"],
            "promoted_from_id": row["promoted_from_id"],
            "promoted_from_team": row["promoted_from_team"],
        },
        "commit_rows": [
            {**r, "committed_at": r["committed_at"].isoformat()} for r in commit_rows
        ],
    }


# ---------------------------------------------------------------------------
# Memory (semantic, episodic, working)
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/semantic")
async def admin_semantic(
    request: Request, team: str = TeamPath, q: str | None = None,
    cursor: str | None = None, limit: int = 50, count: bool = False,
    _op: AdminContext = CurrentOperator,
):
    filters: dict[str, Any] = {}
    for key, val in request.query_params.multi_items():
        if key.startswith("meta."):
            filters[key[5:]] = val

    if q:
        qvec = (await State.embedder.embed([q]))[0]
        rows = await State.falkor.query_semantic(team, qvec, limit, filters)
        return {"items": rows, "next_cursor": None, "total": None}

    decoded = decode_cursor(cursor) if cursor else None
    rows = await State.falkor.list_semantic(team, filters=filters, cursor=decoded, limit=limit)
    items = [{"id": r["id"], "content": r["content"], "metadata": r["metadata"], "score": 0.0}
             for r in rows]
    next_cursor = _next_cursor(
        rows, limit, lambda r: {"created_at": r["created_at"], "id": r["id"]})
    total = await State.falkor.count_semantic(team, filters=filters) if count else None
    return {"items": items, "next_cursor": next_cursor, "total": total}


@router.get("/teams/{team}/episodic")
async def admin_episodic(
    team: str = TeamPath, since_ts: str | None = None, since_id: str | None = None,
    cursor: str | None = None, limit: int = 500, event_type: str | None = None,
    agent_id: str | None = None, order: str = "asc", archived: str = "false",
    _op: AdminContext = CurrentOperator,
):
    """`read_episodic` with `archived`, an opaque `cursor` (or the raw
    since_ts/since_id pair), and the list envelope."""
    if cursor is not None:
        if since_ts is not None or since_id is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="pass cursor or since_ts/since_id, not both")
        decoded = decode_cursor(cursor)
        since_ts, since_id = decoded.get("ts"), decoded.get("id")
        if since_ts is None or since_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="malformed cursor")
    if (since_ts is None) != (since_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="since_ts and since_id must be provided together")
    if order not in ("asc", "desc"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"order must be 'asc' or 'desc', got {order!r}")
    if archived not in ("false", "true", "any"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"archived must be 'false', 'true', or 'any', got {archived!r}")
    parsed_ts = None
    if since_ts is not None:
        try:
            parsed_ts = datetime.datetime.fromisoformat(since_ts)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"since_ts is not a valid ISO8601 timestamp: {since_ts!r}")

    rows = await State.pg.read_episodic(
        team, since_ts=parsed_ts, since_id=since_id, limit=limit, event_type=event_type,
        agent_id=agent_id, order=order, archived=archived)
    items = [
        {"id": str(r["id"]), "agent_id": r["agent_id"], "session_id": r["session_id"],
         "event_type": r["event_type"], "event_data": r["event_data"],
         "occurred_at": r["occurred_at"].isoformat(), "archived": r["archived"]}
        for r in rows
    ]
    next_cursor = _next_cursor(
        items, limit, lambda r: {"ts": r["occurred_at"], "id": r["id"]})
    return {"items": items, "next_cursor": next_cursor, "total": None}


@router.get("/teams/{team}/working")
async def admin_working(team: str = TeamPath, session: str = "",
                        _op: AdminContext = CurrentOperator):
    if not session:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="session is required")
    return {"items": await State.falkor.working_list(team, session)}


# ---------------------------------------------------------------------------
# The commit log
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/commit-log")
async def admin_commit_log(
    team: str = TeamPath, object_id: str | None = None, object_type: str | None = None,
    operation: str | None = None, source_kind: str | None = None,
    gate_decision: str | None = None, since_seq: int | None = None,
    cursor: str | None = None, order: str = "asc", limit: int = 100,
    count: bool = False, _op: AdminContext = CurrentOperator,
):
    """Takes an opaque `cursor` or a raw `since_seq`."""
    if cursor is not None:
        if since_seq is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="pass cursor or since_seq, not both")
        decoded = decode_cursor(cursor)
        since_seq = decoded.get("seq")
        if not isinstance(since_seq, int):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="malformed cursor")
    filters = {"object_id": object_id, "object_type": object_type, "operation": operation,
              "source_kind": source_kind, "gate_decision": gate_decision}
    rows = await State.pg.read_commit_log(team, filters=filters, since_seq=since_seq,
                                          order=order, limit=limit)
    items = [{**r, "committed_at": r["committed_at"].isoformat()} for r in rows]
    next_cursor = _next_cursor(rows, limit, lambda r: {"seq": r["seq"]})
    total = await State.pg.count_commit_log(team, filters=filters) if count else None
    return {"items": items, "next_cursor": next_cursor, "total": total}


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/proposals")
async def admin_proposals(
    team: str = TeamPath, resolved: str = "false", resolution: str | None = None,
    object_type: str | None = None, source_kind: str | None = None,
    cursor: str | None = None, limit: int = 50,
    count: bool = False, _op: AdminContext = CurrentOperator,
):
    """`source_kind` is bulk reject's filter, so this shows (and with `count=true`
    counts) what a bulk rejection would resolve."""
    decoded = None
    if cursor:
        raw = decode_cursor(cursor)
        try:
            decoded = {"created_at": datetime.datetime.fromisoformat(raw["created_at"]),
                      "id": raw["id"]}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="malformed cursor")

    rows = await State.pg.list_proposals(team, resolved=resolved, resolution=resolution,
                                         object_type=object_type, source_kind=source_kind,
                                         cursor=decoded, limit=limit)
    items = [
        {"proposal_id": str(r["proposal_id"]), "object_type": r["object_type"],
         "proposed_by": r["proposed_by"], "created_at": r["created_at"].isoformat(),
         "summary": summarize_proposal(r["object_type"], r["body"]), "body": r["body"],
         "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
         "resolution": r["resolution"], "resolved_by": r["resolved_by"],
         "resolved_via": r["resolved_via"], "reject_reason": r["reject_reason"]}
        for r in rows
    ]
    next_cursor = _next_cursor(
        items, limit, lambda r: {"created_at": r["created_at"], "id": r["proposal_id"]})
    total = (await State.pg.count_proposals(
        team, resolved=resolved, resolution=resolution, object_type=object_type,
        source_kind=source_kind)
        if count else None)
    return {"items": items, "next_cursor": next_cursor, "total": total}


# ---------------------------------------------------------------------------
# The code graph: proxies over falkor_code.py, without the episodic emit
# /v1/code adds.
# ---------------------------------------------------------------------------

@router.get("/teams/{team}/code/repos")
async def admin_code_repos(team: str = TeamPath, _op: AdminContext = CurrentOperator):
    return sorted(await State.falkor.code_indexed_repos(team))


@router.get("/teams/{team}/code/search", response_model=list[CodeFunction])
async def admin_code_search(
    team: str = TeamPath, q: str | None = None, name: str | None = None,
    repos: list[str] | None = Query(None), limit: int = 10,
    _op: AdminContext = CurrentOperator,
):
    qvec = None
    if q:
        embedded = await best_effort(lambda: State.embedder.embed([q]), fallback=None,
                                     what=f"admin code_search embed (q={q!r})")
        qvec = embedded[0] if embedded else None
    rows = await State.falkor.code_search(team, qvec=qvec, name=name, repos=repos, limit=limit)
    return [CodeFunction(**r) for r in rows]


@router.get("/teams/{team}/code/functions/{fn_id}/trace", response_model=list[CodeFunction])
async def admin_code_trace(
    fn_id: str, team: str = TeamPath, direction: str = "outbound", depth: int = 2,
    min_confidence: float = 0.0, _op: AdminContext = CurrentOperator,
):
    if direction not in ("outbound", "inbound"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="direction must be 'outbound' or 'inbound'")
    rows = await State.falkor.code_trace_calls(team, fn_id=fn_id, direction=direction,
                                               depth=depth, min_confidence=min_confidence)
    return [CodeFunction(**r) for r in rows]


@router.get("/teams/{team}/code/architecture", response_model=CodeArchitecture)
async def admin_code_architecture(
    team: str = TeamPath, repos: list[str] | None = Query(None),
    _op: AdminContext = CurrentOperator,
):
    arch = await State.falkor.code_architecture(team, repos=repos)
    return CodeArchitecture(**arch)


@router.get("/teams/{team}/code/changes", response_model=list[CodeChange])
async def admin_code_changes(
    team: str = TeamPath, repo: str = "", commit: str = "",
    _op: AdminContext = CurrentOperator,
):
    rows = await State.falkor.code_changed_since(team, commit=commit, repo=repo)
    return [CodeChange(**r) for r in rows]


# ---------------------------------------------------------------------------
# Console administration (kwim_admin; `team` is column data, not an identifier)
# ---------------------------------------------------------------------------

@router.get("/keys")
async def admin_keys(team: str | None = None, include_revoked: bool = False,
                     _op: AdminContext = CurrentOperator):
    """`list_api_keys` already excludes `key_hash` from its projection -
    reused as-is."""
    return {"items": await State.admin.list_api_keys(team=team, include_revoked=include_revoked)}


@router.get("/operators")
async def admin_operators(_op: AdminContext = CurrentOperator):
    return {"items": await State.admin.list_operators()}


@router.get("/audit")
async def admin_audit(
    team: str | None = None, operator: str | None = None, action: str | None = None,
    since: str | None = None, cursor: str | None = None, limit: int = 100,
    _op: AdminContext = CurrentOperator,
):
    parsed_since = None
    if since:
        try:
            parsed_since = datetime.datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"since is not a valid ISO8601 timestamp: {since!r}")
    decoded_seq = None
    if cursor:
        raw = decode_cursor(cursor)
        try:
            decoded_seq = int(raw["seq"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="malformed cursor")

    rows = await State.admin.list_audit(team=team, operator_id=operator, action=action,
                                        since=parsed_since, cursor=decoded_seq, limit=limit)
    items = [{**r, "at": r["at"].isoformat()} for r in rows]
    next_cursor = _next_cursor(items, limit, lambda r: {"seq": r["seq"]})
    return {"items": items, "next_cursor": next_cursor, "total": None}
