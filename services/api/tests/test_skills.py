"""Tests for skills: the SKILL.md format and its checks, search (ranking, the
floor, persona, scope, the full-text fallback), proposal validation, the routes'
capabilities, and the proposal models."""
import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from kwim_api.config import settings
from kwim_api.gate import summarize_proposal
from kwim_api.models import PendingProposal, ProposalStatus
from kwim_api.runtime import State
from kwim_api.skills import service
from kwim_api.skills.format import SkillFormatError, parse_skill


@contextmanager
def _setting(name, value):
    """Override a frozen setting for one test."""
    old = getattr(settings, name)
    object.__setattr__(settings, name, value)
    try:
        yield
    finally:
        object.__setattr__(settings, name, old)


def _skill_md(name="check-host-cpu", description="Check a host's CPU usage.", body="Run it.",
              extra=""):
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n"


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------

def test_parse_reads_frontmatter_lists_and_body():
    s = parse_skill(_skill_md(extra="agents: ops, coder\ntags: host, cpu\n"
                                    "refs: org/repo:playbooks/report.yml\n"))
    assert (s.name, s.agents, s.tags, s.refs, s.body) == (
        "check-host-cpu", ["ops", "coder"], ["host", "cpu"],
        ["org/repo:playbooks/report.yml"], "Run it.")


@pytest.mark.parametrize("text, fragment", [
    ("no fence\nname: x", "frontmatter fence"),
    ("---\nname: x\ndescription: y\nbody", "unterminated"),
    (_skill_md(name="Bad_Name"), "lowercase letters"),
    (_skill_md(extra="color: red\n"), "unknown frontmatter key"),
    (_skill_md(body=""), "no instructions"),
    (_skill_md(extra="refs: playbooks/x.yml\n"), "refs must be"),
    (_skill_md(extra="refs: org/repo:../../etc/passwd\n"), "refs must be"),
    (_skill_md(body="password: hunter2hunter2"), "credential"),
    (_skill_md(body="-----BEGIN " "OPENSSH PRIVATE KEY-----"), "credential"),
    (_skill_md(body="$SECRETS;1.1;AES256\n6162"), "credential"),
])
def test_parse_refuses(text, fragment):
    with pytest.raises(SkillFormatError, match=fragment):
        parse_skill(text)


def test_parse_allows_references_to_where_secrets_live():
    parse_skill(_skill_md(body="run-report --password-file ~/.ssh/report-password\n"
                               "password: <from the secret store>\ntoken: {{ report_token }}"))


def test_parse_refuses_oversized_text():
    with _setting("skills_max_chars", 50), pytest.raises(SkillFormatError, match="limit is 50"):
        parse_skill(_skill_md(body="x" * 60))


def test_credential_error_does_not_echo_the_value():
    with pytest.raises(SkillFormatError) as exc:
        parse_skill(_skill_md(body="password: hunter2hunter2"))
    assert "hunter2" not in str(exc.value)


# ---------------------------------------------------------------------------
# In-memory store and embedder
# ---------------------------------------------------------------------------

def _row(name, *, embedding, agents=(), tags=(), fts=0.0, hit=False, status="current",
         body="Run it.", description=None):
    return {"id": uuid.uuid4(), "name": name, "status": status, "current_version": 1,
            "version": 1, "owner": "op", "description": description or f"{name} skill",
            "body": body, "agents": list(agents), "tags": list(tags), "refs": [],
            "text": _skill_md(name=name), "embedding": embedding, "fts_rank": fts,
            "fts_hit": hit, "promoted_to": None, "promoted_from_team": None,
            "promoted_from_id": None, "created_at": datetime.now(UTC),
            "created_by": "op", "updated_at": datetime.now(UTC), "author": "op"}


class FakePg:
    def __init__(self, skills: dict[str, list[dict]], pending=None):
        self.skills = skills
        self.pending = pending or {}
        self.embedded: list[tuple] = []
        self.inserted: list[tuple] = []

    async def skill_search_rows(self, team, q):
        return [dict(r) for r in self.skills.get(team, []) if r["status"] == "current"]

    async def get_skill(self, team, name):
        return next((r for r in self.skills.get(team, []) if r["name"] == name), None)

    async def set_skill_embedding(self, team, skill_id, version, vec):
        self.embedded.append((team, skill_id, version))

    async def pending_skill_proposals(self, team):
        return self.pending.get(team, {})

    async def insert_pending(self, team, row):
        self.inserted.append((team, row))


class FakeEmbedder:
    def __init__(self, query_vec, fail=False):
        self.query_vec, self.fail = query_vec, fail

    async def embed(self, texts):
        if self.fail:
            raise RuntimeError("embedder down")
        return [self.query_vec] + [[0.0, 1.0] for _ in texts[1:]]


class FakeFalkor:
    async def proposal_set(self, pid, doc):
        pass


def _search(pg, embedder, team="acme", q="cpu", **kw):
    return asyncio.run(service.search(pg, embedder, team, q, limit=5, **kw))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_ranks_by_similarity_and_applies_the_floor():
    pg = FakePg({"acme": [_row("near", embedding=[1.0, 0.1]),
                          _row("nearer", embedding=[1.0, 0.0]),
                          _row("far", embedding=[0.0, 1.0])]})
    with _setting("skills_search_min_similarity", 0.6):
        res = _search(pg, FakeEmbedder([1.0, 0.0]))
    assert [r["name"] for r in res["results"]] == ["nearer", "near"]
    assert res["coverage"] == {"available": True, "vector": True, "fulltext": False, "reason": None}


def test_search_filters_by_persona():
    pg = FakePg({"acme": [_row("ops-skill", embedding=[1.0, 0.0], agents=["ops"]),
                          _row("coder-skill", embedding=[1.0, 0.0], agents=["coder"]),
                          _row("everyone", embedding=[1.0, 0.0])]})
    names = {r["name"] for r in _search(pg, FakeEmbedder([1.0, 0.0]), agent="ops")["results"]}
    assert names == {"ops-skill", "everyone"}


def test_team_skill_hides_universe_skill_of_the_same_name():
    pg = FakePg({"acme": [_row("check-disk", embedding=[1.0, 0.0])],
                 "universe": [_row("check-disk", embedding=[1.0, 0.0]),
                              _row("shared", embedding=[1.0, 0.0])]})
    res = _search(pg, FakeEmbedder([1.0, 0.0]))
    assert sorted((r["name"], r["scope"]) for r in res["results"]) == [
        ("check-disk", "team"), ("shared", "universe")]


def test_search_falls_back_to_full_text_when_the_embedder_fails():
    pg = FakePg({"acme": [_row("hit-low", embedding=[1.0, 0.0], fts=0.1, hit=True),
                          _row("hit-high", embedding=[1.0, 0.0], fts=0.5, hit=True),
                          _row("no-hit", embedding=[1.0, 0.0])]})
    res = _search(pg, FakeEmbedder([1.0, 0.0], fail=True))
    assert [r["name"] for r in res["results"]] == ["hit-high", "hit-low"]
    assert res["coverage"]["vector"] is False and res["coverage"]["fulltext"] is True
    assert "embedder unavailable" in res["coverage"]["reason"]


def test_search_embeds_versions_missing_an_embedding():
    pg = FakePg({"acme": [_row("unembedded", embedding=None)]})
    _search(pg, FakeEmbedder([0.0, 1.0]))
    assert [(t, v) for t, _, v in pg.embedded] == [("acme", 1)]


def test_search_reports_a_pending_revision():
    pending = {"acme": {"check-host-cpu": {"proposal_id": uuid.uuid4(), "proposed_by": "devkey",
                                           "created_at": datetime.now(UTC),
                                           "description": "newer"}}}
    pg = FakePg({"acme": [_row("check-host-cpu", embedding=[1.0, 0.0])]}, pending)
    result = _search(pg, FakeEmbedder([1.0, 0.0]))["results"][0]
    assert result["pending_revision"]["proposed_by"] == "devkey"
    assert result["pending_revision"]["summary"] == "newer"


def test_get_returns_the_team_skill_before_the_universe_one_and_skips_retired():
    pg = FakePg({"acme": [_row("a", embedding=None, status="retired")],
                 "universe": [_row("a", embedding=None, body="universe body")]})
    got = asyncio.run(service.get(pg, "acme", "a"))
    assert (got["scope"], got["body"]) == ("universe", "universe body")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(service.get(pg, "acme", "missing"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Proposal validation
# ---------------------------------------------------------------------------

def _propose(pg, text, **kw):
    kw.setdefault("supersedes", None)
    kw.setdefault("scope", "team")
    return asyncio.run(service.propose(pg, FakeFalkor(), None, "acme", text,
                                       proposed_by="devkey", **kw))


@pytest.mark.parametrize("skills, text, kw, code", [
    ({"acme": [_row("check-host-cpu", embedding=None)]}, _skill_md(), {}, 409),
    ({"acme": [_row("check-host-cpu", embedding=None)]}, _skill_md(name="other"),
     {"supersedes": "check-host-cpu"}, 422),
    ({}, _skill_md(), {"supersedes": "check-host-cpu"}, 404),
    ({"acme": [_row("check-host-cpu", embedding=None, status="retired")]}, _skill_md(),
     {"supersedes": "check-host-cpu"}, 409),
    ({}, _skill_md(), {"scope": "universe"}, 422),
    ({}, _skill_md(body="token: abcdefghijklmnop"), {}, 422),
])
def test_propose_refuses(skills, text, kw, code):
    pg = FakePg(skills)
    with pytest.raises(HTTPException) as exc:
        _propose(pg, text, **kw)
    assert exc.value.status_code == code
    assert pg.inserted == []


def test_propose_queues_a_universe_revision_in_the_universe():
    pg = FakePg({"universe": [_row("check-host-cpu", embedding=None)]})
    result = _propose(pg, _skill_md(), supersedes="check-host-cpu", scope="universe")
    assert result["queue"] == "universe" and result["status"] == "pending_review"
    team, row = pg.inserted[0]
    assert team == "universe" and row["object_type"] == "skill"
    assert row["body"]["owner"] == "op" and row["body"]["proposing_team"] == "acme"


# ---------------------------------------------------------------------------
# Routes, gate summary, models
# ---------------------------------------------------------------------------

def test_search_route_returns_results(client, monkeypatch):
    monkeypatch.setattr(State, "pg", FakePg({"acme": [_row("check-host-cpu", embedding=[1.0, 0.0])]}),
                        raising=False)
    monkeypatch.setattr(State, "docs_embedder", FakeEmbedder([1.0, 0.0]), raising=False)
    r = client.get("/v1/skills/search", params={"q": "cpu", "agent": "ops"},
                   headers={"Authorization": "Bearer devkey"})
    assert r.status_code == 200
    assert r.json()["results"][0]["name"] == "check-host-cpu"


def test_promote_route_requires_promote(client):
    r = client.post("/v1/skills/check-host-cpu/promote",
                    headers={"Authorization": "Bearer devkey"})
    assert r.status_code == 403


def test_summary_names_the_owner():
    summary = summarize_proposal("skill", {"name": "check-host-cpu", "owner": "alice",
                                           "description": "CPU", "supersedes": "check-host-cpu"})
    assert summary == "revision of check-host-cpu (owner: alice): CPU"
    assert "owner: none" in summarize_proposal("skill", {"name": "x", "description": "y"})


def test_proposal_models_accept_skill():
    PendingProposal(proposal_id="p", object_type="skill", proposed_by=None,
                    created_at=datetime.now(UTC), summary="s", body={})
    ProposalStatus(id="p", object_type="skill", status="pending_review")
