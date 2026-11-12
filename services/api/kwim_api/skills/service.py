"""Skill writes, proposals, promotion, search and get. Used by the admin routes,
the agent routes and the gate (which commits approved proposals).
"""
import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import HTTPException, status

from ..config import settings
from .format import ParsedSkill, SkillFormatError, parse_skill

log = logging.getLogger(__name__)

UNIVERSE = "universe"


def _parse(text: str) -> ParsedSkill:
    try:
        return parse_skill(text)
    except SkillFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"SKILL.md {exc}")


def _embed_text(name: str, description: str, body: str) -> str:
    """What a skill is embedded as: the whole skill, not only its description,
    which ranked better in the evaluation."""
    return f"{name}: {description}\n\n{body}"


async def _embed_one(embedder: Any, text: str) -> list[float] | None:
    """One embedding, or None when there is no embedder or it fails."""
    if embedder is None:
        return None
    try:
        return (await embedder.embed([text]))[0]
    except Exception as exc:
        log.warning("skills: embedding failed (%s); it is retried on the next search", exc)
        return None


async def _version(parsed: ParsedSkill, text: str, embedder: Any, *, source: str,
                   author: str | None, approver: str | None,
                   proposal_id: str | None = None) -> dict[str, Any]:
    return {
        "text": text, "description": parsed.description, "body": parsed.body,
        "agents": parsed.agents, "tags": parsed.tags, "refs": parsed.refs,
        "embedding": await _embed_one(
            embedder, _embed_text(parsed.name, parsed.description, parsed.body)),
        "source": source, "author": author, "approver": approver, "proposal_id": proposal_id,
    }


async def _log(pg: Any, team: str, skill: dict[str, Any], operation: str, *,
               provenance: dict[str, Any], proposed_by: str | None = None,
               source_kind: str | None = None) -> int:
    return await pg.append_commit(team, {
        "object_type": "skill", "object_id": str(skill["id"]), "operation": operation,
        "payload": {"name": skill["name"], "version": skill["current_version"],
                    "description": skill["description"]},
        "provenance": {**provenance, "at": datetime.now(UTC).isoformat()},
        "proposed_by": proposed_by, "source_kind": source_kind,
        "gate_decision": "human_approved",
    })


async def _get_or_404(pg: Any, team: str, name: str) -> dict[str, Any]:
    skill = await pg.get_skill(team, name)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"skill {name!r} not found")
    return skill


# --- Human writes (admin) ------------------------------------------------------

async def create(pg: Any, embedder: Any, team: str, text: str, *, by: str, via: str,
                 owner: str | None = None) -> dict[str, Any]:
    """A new skill, committed directly. The author owns it unless `owner` is given."""
    parsed = _parse(text)
    if await pg.get_skill(team, parsed.name) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"skill {parsed.name!r} exists; revise it instead")
    version = await _version(parsed, text, embedder, source="human", author=by, approver=by)
    try:
        skill = await pg.create_skill(team, name=parsed.name, owner=owner or by,
                                      created_by=by, version=version)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"skill {parsed.name!r} exists; revise it instead")
    seq = await _log(pg, team, skill, "commit", provenance={"author": by, "via": via})
    return {"skill": skill, "seq": seq}


async def revise(pg: Any, embedder: Any, team: str, name: str, text: str, *, by: str,
                 via: str) -> dict[str, Any]:
    """A new version of a skill, committed directly. Its name cannot change."""
    skill = await _get_or_404(pg, team, name)
    parsed = _parse(text)
    if parsed.name != name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"the text names {parsed.name!r}, not {name!r}; "
                                   "a revision keeps the skill's name")
    version = await _version(parsed, text, embedder, source="human", author=by, approver=by)
    skill = await pg.add_skill_version(team, str(skill["id"]), version)
    seq = await _log(pg, team, skill, "revise", provenance={"author": by, "via": via})
    return {"skill": skill, "seq": seq}


async def set_status(pg: Any, team: str, name: str, new_status: str, *, by: str,
                     via: str) -> dict[str, Any]:
    """Retire (`retired`) or restore (`current`) a skill. Its versions are kept."""
    skill = await _get_or_404(pg, team, name)
    if skill["status"] == new_status:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"skill {name!r} is already {new_status}")
    await pg.set_skill_status(team, str(skill["id"]), new_status)
    skill = {**skill, "status": new_status}
    operation = "retire" if new_status == "retired" else "restore"
    seq = await _log(pg, team, skill, operation, provenance={"by": by, "via": via})
    return {"skill": skill, "seq": seq}


async def set_owner(pg: Any, team: str, name: str, owner: str | None) -> dict[str, Any]:
    skill = await _get_or_404(pg, team, name)
    await pg.set_skill_owner(team, str(skill["id"]), owner)
    return {**skill, "owner": owner}


# --- Agent proposals -------------------------------------------------------------

async def propose(pg: Any, falkor: Any, gate: Any, team: str, text: str, *,
                  supersedes: str | None, scope: str, proposed_by: str) -> dict[str, Any]:
    """Queue an agent's new skill or revision for review. A universe revision
    goes to the universe's queue. Never commits."""
    parsed = _parse(text)
    target = UNIVERSE if scope == "universe" else team
    if supersedes:
        current = await _get_or_404(pg, target, supersedes)
        if parsed.name != supersedes:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"the text names {parsed.name!r}, not {supersedes!r}; "
                                       "a revision keeps the skill's name")
        if current["status"] != "current":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail=f"skill {supersedes!r} is retired")
    else:
        if scope == "universe":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="a new skill is proposed to the team; the universe "
                                       "takes revisions and promotions")
        if await pg.get_skill(target, parsed.name) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail=f"skill {parsed.name!r} exists; propose a revision "
                                       "with supersedes")
    return await _queue(pg, falkor, gate, target, {
        "name": parsed.name, "description": parsed.description, "text": text,
        "supersedes": supersedes, "proposing_team": team,
    }, proposed_by=proposed_by)


async def _queue(pg: Any, falkor: Any, gate: Any, target: str, body: dict[str, Any], *,
                 proposed_by: str) -> dict[str, Any]:
    owner = None
    if body.get("supersedes"):
        current = await pg.get_skill(target, body["supersedes"])
        owner = current["owner"] if current else None
    body = {**body, "owner": owner}
    pid = str(uuid.uuid4())
    proposal = {"team": target, "proposal_id": pid, "object_type": "skill",
                "proposed_by": proposed_by, "body": body}
    await pg.insert_pending(target, {"proposal_id": pid, "object_type": "skill",
                                     "proposed_by": proposed_by, "body": body,
                                     "bus_message": proposal})
    await falkor.proposal_set(pid, {"id": pid, "object_type": "skill",
                                    "status": "pending_review",
                                    "detail": "queued for human review"})
    if gate is not None:
        await gate._notify_review(target, pid, "skill", body)
    return {"proposal_id": pid, "status": "pending_review", "queue": target}


async def commit_proposal(pg: Any, embedder: Any, team: str, pid: str,
                          body: dict[str, Any], proposed_by: str | None,
                          provenance: dict[str, Any]) -> dict[str, Any]:
    """Commit an approved skill proposal in `team` (the queue it was in). A
    proposal for a name that exists becomes its next version. A new skill is
    owned by the approving operator when approved in the admin console."""
    text = body["text"]
    parsed = parse_skill(text)
    approver = provenance.get("approved_by")
    source = "promotion" if body.get("promote_from_team") else "agent_proposal"
    version = await _version(parsed, text, embedder, source=source,
                             author=proposed_by, approver=approver, proposal_id=pid)
    existing = await pg.get_skill(team, parsed.name)
    if existing is not None:
        skill = await pg.add_skill_version(team, str(existing["id"]), version)
        operation = "revise"
    else:
        owner = approver if provenance.get("approved_via") == "admin_console" else None
        skill = await pg.create_skill(
            team, name=parsed.name, owner=owner, created_by=proposed_by, version=version,
            promoted_from_team=body.get("promote_from_team"),
            promoted_from_id=body.get("promote_from_id"))
        operation = "commit"
    seq = await _log(pg, team, skill, operation,
                     provenance={"proposed_by": proposed_by, "proposal_id": pid,
                                 **({"promoted_from_team": body["promote_from_team"]}
                                    if body.get("promote_from_team") else {}),
                                 **provenance},
                     proposed_by=proposed_by, source_kind=(
                         "promotion" if source == "promotion" else "agent_proposal"))
    if body.get("promote_from_team") and body.get("promote_from_id"):
        await pg.set_skill_promoted(body["promote_from_team"], body["promote_from_id"],
                                    str(skill["id"]))
    return {"object_id": str(skill["id"]), "seq": seq, "version": skill["current_version"]}


# --- Promotion -------------------------------------------------------------------

async def promote(pg: Any, falkor: Any, gate: Any, embedder: Any, team: str, name: str, *,
                  by: str, via: str, operator: bool) -> dict[str, Any]:
    """Copy a team skill's current version into the universe. When the universe
    already has the name, an operator's promotion becomes its next version and a
    team key's promotion is queued for operator review."""
    if team == UNIVERSE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail="universe skills cannot be promoted")
    skill = await _get_or_404(pg, team, name)
    if skill["status"] != "current":
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"skill {name!r} is retired")
    existing = await pg.get_skill(UNIVERSE, name)
    if existing is not None and not operator:
        queued = await _queue(pg, falkor, gate, UNIVERSE, {
            "name": name, "description": skill["description"], "text": skill["text"],
            "supersedes": name, "proposing_team": team,
            "promote_from_team": team, "promote_from_id": str(skill["id"]),
        }, proposed_by=by)
        return {"status": "pending_review", "proposal_id": queued["proposal_id"],
                "detail": f"the universe already has {name!r}; an operator reviews the "
                          "promotion as its next version"}
    parsed = parse_skill(skill["text"])
    version = await _version(parsed, skill["text"], embedder, source="promotion",
                             author=skill["author"], approver=by)
    provenance = {"promoted_from_team": team, "promoted_from_id": str(skill["id"]),
                  "promoted_by": by, "via": via}
    if existing is None:
        copy = await pg.create_skill(UNIVERSE, name=name, owner=skill["owner"], created_by=by,
                                     version=version, promoted_from_team=team,
                                     promoted_from_id=str(skill["id"]))
        operation = "commit"
    else:
        copy = await pg.add_skill_version(UNIVERSE, str(existing["id"]), version)
        operation = "revise"
    seq = await _log(pg, UNIVERSE, copy, operation, provenance=provenance,
                     proposed_by=by, source_kind="promotion")
    await pg.set_skill_promoted(team, str(skill["id"]), str(copy["id"]))
    return {"status": "promoted", "name": name, "universe_version": copy["current_version"],
            "seq": seq}


# --- Reads -----------------------------------------------------------------------

def _pending(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    created = proposal["created_at"]
    return {"proposal_id": str(proposal["proposal_id"]),
            "proposed_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "proposed_by": proposal["proposed_by"], "summary": proposal["description"]}


def _offered(row: dict[str, Any], agent: str | None) -> bool:
    agents = row["agents"] or []
    return agent is None or not agents or agent in agents


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _rows_by_scope(pg: Any, team: str, q: str) -> list[tuple[str, dict[str, Any]]]:
    """Current skills of the team and the universe; a team skill hides a
    universe skill of the same name."""
    team_rows = await pg.skill_search_rows(team, q)
    if team == UNIVERSE:
        return [(UNIVERSE, r) for r in team_rows]
    names = {r["name"] for r in team_rows}
    universe_rows = [r for r in await pg.skill_search_rows(UNIVERSE, q)
                     if r["name"] not in names]
    return [(team, r) for r in team_rows] + [(UNIVERSE, r) for r in universe_rows]


async def search(pg: Any, embedder: Any, team: str, q: str, *, limit: int,
                 agent: str | None = None, tag: str | None = None) -> dict[str, Any]:
    """Skills that match `q`, best first, as names and descriptions."""
    coverage: dict[str, Any] = {"available": True, "vector": False, "fulltext": False,
                                "reason": None}
    rows = [(scope, r) for scope, r in await _rows_by_scope(pg, team, q)
            if _offered(r, agent) and (tag is None or tag in (r["tags"] or []))]

    qvec = None
    if embedder is None:
        coverage["reason"] = "no skills embedder configured; full-text only"
    else:
        try:
            missing = [(scope, r) for scope, r in rows if r["embedding"] is None]
            vectors = await embedder.embed(
                [q] + [_embed_text(r["name"], r["description"], r["body"]) for _, r in missing])
            qvec = vectors[0]
            for (scope, r), vec in zip(missing, vectors[1:], strict=True):
                r["embedding"] = vec
                await pg.set_skill_embedding(scope, str(r["id"]), r["version"], vec)
            coverage["vector"] = True
        except Exception as exc:
            log.warning("skills: search embedding failed (%s); full-text only", exc)
            coverage["reason"] = "embedder unavailable; full-text only"

    # Ranked by similarity to the whole skill. Full-text ranking is used only
    # without an embedder: fused with the vector ranking it ranked worse.
    similarity: dict[int, float] = {}
    coverage["fulltext"] = qvec is None
    if qvec is not None:
        for i, (_, r) in enumerate(rows):
            if r["embedding"] is not None:
                similarity[i] = _cosine(qvec, r["embedding"])
        floor = settings.skills_search_min_similarity
        ranked = sorted((i for i, sim in similarity.items() if sim >= floor),
                        key=lambda i: -similarity[i])[:limit]
        scored = similarity
    else:
        ranked = sorted((i for i, (_, r) in enumerate(rows) if r["fts_hit"]),
                        key=lambda i: -rows[i][1]["fts_rank"])[:limit]
        scored = {i: float(rows[i][1]["fts_rank"]) for i in ranked}

    pending: dict[str, dict[str, Any]] = {}
    for scope in {rows[i][0] for i in ranked}:
        pending[scope] = await pg.pending_skill_proposals(scope)
    results = []
    for i in ranked:
        scope, r = rows[i]
        results.append({
            "name": r["name"], "description": r["description"], "tags": r["tags"],
            "agents": r["agents"], "scope": "universe" if scope == UNIVERSE else "team",
            "version": r["version"], "score": round(scored[i], 6),
            "similarity": round(similarity[i], 4) if i in similarity else None,
            "pending_revision": _pending(pending[scope].get(r["name"])),
        })
    return {"results": results, "coverage": coverage}


async def get(pg: Any, team: str, name: str) -> dict[str, Any]:
    """The skill an agent loads: the team's, else the universe's. Retired skills
    are not returned."""
    for scope in dict.fromkeys((team, UNIVERSE)):
        skill = await pg.get_skill(scope, name)
        if skill is not None and skill["status"] == "current":
            pending = (await pg.pending_skill_proposals(scope)).get(name)
            return {
                "name": skill["name"], "description": skill["description"],
                "agents": skill["agents"], "tags": skill["tags"],
                "scope": "universe" if scope == UNIVERSE else "team",
                "version": skill["current_version"], "body": skill["body"],
                "refs": skill["refs"], "owner": skill["owner"],
                "updated_at": skill["updated_at"].isoformat(),
                "pending_revision": _pending(pending),
            }
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"skill {name!r} not found")


async def list_offered(pg: Any, team: str, *, agent: str | None = None,
                       tag: str | None = None) -> list[dict[str, Any]]:
    """Current skills an agent can load, team and universe, by name."""
    team_rows = await pg.list_skills(team)
    names = {r["name"] for r in team_rows}
    rows = [(team, r) for r in team_rows]
    if team != UNIVERSE:
        rows += [(UNIVERSE, r) for r in await pg.list_skills(UNIVERSE) if r["name"] not in names]
    return [{"name": r["name"], "description": r["description"], "tags": r["tags"],
             "agents": r["agents"], "scope": "universe" if scope == UNIVERSE else "team",
             "version": r["current_version"]}
            for scope, r in sorted(rows, key=lambda t: t[1]["name"])
            if _offered(r, agent) and (tag is None or tag in (r["tags"] or []))]
