"""Tests for the /v1/admin cross-team write API.

Router tests against fakes on `runtime.State` and a fake gate (gate logic is
tested in test_gate.py and test_amend.py). Forget runs the real forget module
against the fake stores.
"""
import datetime
import uuid

import psycopg
import pytest

from kwim_api.main import app
from kwim_api.runtime import State
from tests.test_admin_read import FakeAdminStore as _BaseFakeAdminStore
from tests.test_admin_read import FakeFalkorStore as _BaseFakeFalkorStore
from tests.test_admin_read import FakePgStore as _BaseFakePgStore
from tests.test_admin_read import admin_enabled  # noqa: F401 - fixture reused by name

pytestmark = pytest.mark.usefixtures("admin_enabled")

_NOW = datetime.datetime(2026, 6, 11, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fakes - extend test_admin_read.py's with what the write routes need.
# ---------------------------------------------------------------------------

class FakeAdminStore(_BaseFakeAdminStore):
    """Overrides the base's no-op `record` to actually persist rows: the write
    router's audit rows can be checked."""

    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def record(self, *, operator_id, action, result, team=None,
                     object_type=None, object_id=None, detail=None):
        self.audit_rows.append({
            "seq": len(self.audit_rows) + 1, "at": _NOW, "operator_id": operator_id,
            "action": action, "team": team, "object_type": object_type,
            "object_id": object_id, "detail": detail or {}, "result": result,
        })

    # --- background jobs ---

    async def create_job(self, *, kind, team, operator_id=None, detail=None):
        if any(j["kind"] == kind and j["team"] == team and j["status"] == "running"
              for j in self.jobs):
            raise psycopg.errors.IntegrityError(
                "duplicate key value violates unique constraint "
                '"idx_admin_jobs_one_running"')
        job = {"id": str(uuid.uuid4()), "kind": kind, "team": team, "status": "running",
              "started_at": _NOW, "finished_at": None, "operator_id": operator_id,
              "detail": detail or {}}
        self.jobs.append(job)
        return dict(job)

    async def get_job(self, job_id):
        for j in self.jobs:
            if j["id"] == job_id:
                return dict(j)
        return None

    async def list_jobs(self, *, team=None, status=None, limit=50):
        rows = self.jobs
        if team is not None:
            rows = [j for j in rows if j["team"] == team]
        if status is not None:
            rows = [j for j in rows if j["status"] == status]
        return [dict(j) for j in rows[:limit]]

    async def finish_job(self, job_id, status, detail=None):
        for j in self.jobs:
            if j["id"] == job_id:
                j["status"] = status
                j["finished_at"] = _NOW
                if detail is not None:
                    j["detail"] = detail

    async def fail_orphaned_jobs(self):
        n = 0
        for j in self.jobs:
            if j["status"] == "running":
                j["status"] = "failed"
                j["finished_at"] = _NOW
                j["detail"] = {"error": "service restarted"}
                n += 1
        return n


class FakePgStore(_BaseFakePgStore):
    """Adds proposal-claim, bulk-reject, and forget-preflight/delete support."""

    def __init__(self):
        self.deleted_episodic_ids: list[str] = []
        super().__init__()
        self._preflight_ok: dict[str, bool] = {}

    def _pending_row(self, team, proposal_id):
        for r in self.proposals.get(team, []):
            if r["proposal_id"] == proposal_id:
                return r
        return None

    async def get_pending(self, team, proposal_id):
        row = self._pending_row(team, proposal_id)
        return dict(row) if row else None

    async def claim_pending(self, team, proposal_id, resolution, resolved_by,
                            resolved_via, reject_reason=None):
        row = self._pending_row(team, proposal_id)
        if row is None or row["resolved_at"] is not None:
            return None
        row["resolved_at"] = _NOW
        row["resolution"] = resolution
        row["resolved_by"] = resolved_by
        row["resolved_via"] = resolved_via
        row["reject_reason"] = reject_reason
        return dict(row)

    async def reject_pending(self, team, *, source_kind=None, reason="bulk cleanup",
                             resolved_by="cleanup", resolved_via="api",
                             older_than=None):
        rows = [r for r in self.proposals.get(team, []) if r["resolved_at"] is None]
        if source_kind:
            rows = [r for r in rows if r["body"].get("source_kind") == source_kind]
        if older_than is not None:
            rows = [r for r in rows if r["created_at"] < older_than]
        for r in rows:
            r["resolved_at"] = _NOW
            r["resolution"] = "rejected"
            r["resolved_by"] = resolved_by
            r["resolved_via"] = resolved_via
            r["reject_reason"] = reason
        return len(rows)

    # --- forget preflight/delete ---

    def seed_preflight(self, team: str, *, ok: bool, role: str = "kwim_app") -> None:
        self._preflight_ok[team] = ok
        self._preflight_role = role

    async def delete_preflight(self, team):
        ok = self._preflight_ok.get(team, True)
        role = getattr(self, "_preflight_role", "kwim_app")
        return {"role": role, "commit_log": ok, "episodic": ok, "verifications": ok}

    async def delete_commit_log(self, team, object_id):
        rows = self.commit_log.get(team, [])
        kept = [r for r in rows if r["object_id"] != object_id]
        n = len(rows) - len(kept)
        self.commit_log[team] = kept
        return n

    async def delete_verifications(self, team, fact_ids):
        return len(fact_ids)

    async def delete_episodic(self, team, episodic_ids):
        rows = self.episodic.get(team, [])
        kept = [r for r in rows if r["id"] not in episodic_ids]
        n = len(rows) - len(kept)
        self.episodic[team] = kept
        return n


class FakeFalkorStore(_BaseFakeFalkorStore):
    """Adds the forget-plan resolution methods and the forget-preview KV store."""

    def __init__(self):
        super().__init__()
        self._forget_objects: dict[tuple, dict] = {}
        self._supported_by: dict[tuple, list[str]] = {}
        self._previews: dict[str, dict] = {}
        self.forgotten_nodes: list[tuple] = []
        self.proposal_sets: list[dict] = []

    def seed_forget_object(self, team, *, id, type, status="current", label="s",
                           evidence=None):
        self._forget_objects[(team, id)] = {
            "id": id, "type": type, "status": status, "label": label,
            "evidence": list(evidence or []),
        }

    def seed_supported_by(self, team, episodic_id, object_ids):
        self._supported_by[(team, episodic_id)] = list(object_ids)

    async def get_object_for_forget(self, team, object_id, object_type=None):
        return self._forget_objects.get((team, object_id))

    async def objects_supported_by(self, team, episodic_id):
        return list(self._supported_by.get((team, episodic_id), []))

    async def forget_node(self, team, object_type, object_id):
        self._forget_objects.pop((team, object_id), None)
        self.forgotten_nodes.append((team, object_type, object_id))

    async def select_forget_ids(self, team, *, object_type, fact_type=None,
                                source_kind=None, status=None,
                                statement_contains=None):
        out = []
        for (t, oid), obj in self._forget_objects.items():
            if t != team or obj["type"] != object_type:
                continue
            if status is not None and obj["status"] != status:
                continue
            if statement_contains and statement_contains not in (obj.get("label") or ""):
                continue
            out.append(oid)
        return out

    async def forget_preview_set(self, token, doc, ttl):
        self._previews[token] = doc

    async def forget_preview_getdel(self, token):
        return self._previews.pop(token, None)

    async def proposal_set(self, proposal_id, doc):
        self.proposal_sets.append(doc)


class FakeGate:
    """Mirrors test_review.py's _FakeGate: every method the write router calls
    on `request.app.state.gate`."""

    def __init__(self):
        self.commit_calls: list[dict] = []
        self.amend_calls: list[dict] = []
        self.retract_calls: list[dict] = []
        self.confirm_calls: list[dict] = []
        self.semantic_calls: list[dict] = []
        self.review_calls: list[dict] = []
        # object_id -> {"type": ..., "status": ...} - drives amend/retract/confirm.
        self.objects: dict[str, dict] = {}
        self.raise_on: str | None = None
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def commit_proposal(self, team, pid, ptype, body, proposal,
                              gate_decision="auto_committed", extra_provenance=None):
        if self.raise_on == "commit_proposal":
            raise RuntimeError("boom")
        self.commit_calls.append({
            "team": team, "pid": pid, "ptype": ptype, "body": body,
            "proposal": proposal, "gate_decision": gate_decision,
            "extra_provenance": extra_provenance,
        })
        object_id = body.get("object_id") or f"obj-{self._next_seq()}"
        return {"id": pid, "object_type": ptype, "status": "committed",
                "object_id": object_id, "seq": self._seq}

    async def _route_to_review(self, team, pid, ptype, body, proposal, detail=None):
        self.review_calls.append({"team": team, "pid": pid, "ptype": ptype,
                                  "body": body, "proposal": proposal})

    async def amend_object(self, team, object_id, object_type, new_payload,
                           amended_by, amended_via, reason=None):
        if self.raise_on == "amend_object":
            raise RuntimeError("boom")
        self.amend_calls.append({
            "team": team, "object_id": object_id, "object_type": object_type,
            "new_payload": new_payload, "amended_by": amended_by,
            "amended_via": amended_via, "reason": reason,
        })
        obj = self.objects.get(object_id)
        if obj is None:
            return {"status": "not_found", "object_id": object_id}
        live = {"fact": "current", "rule": "approved"}.get(object_type)
        if live is not None and obj["status"] != live:
            return {"status": "not_current", "object_id": object_id,
                    "object_status": obj["status"]}
        return {"status": "amended", "object_id": object_id,
                "seq": self._next_seq(), "previous_payload": {}}

    async def retract_object(self, team, object_id, by, via, object_type=None):
        self.retract_calls.append({"team": team, "object_id": object_id, "by": by,
                                   "via": via, "object_type": object_type})
        obj = self.objects.get(object_id)
        if obj is None:
            return {"status": "not_found"}
        if obj["status"] == "retracted":
            return {"status": "already_retracted"}
        obj["status"] = "retracted"
        return {"status": "retracted", "object_id": object_id,
                "object_type": obj["type"], "seq": self._next_seq()}

    async def confirm_object(self, team, object_id, by, via, object_type=None):
        self.confirm_calls.append({"team": team, "object_id": object_id, "by": by,
                                   "via": via, "object_type": object_type})
        obj = self.objects.get(object_id)
        if obj is None:
            return {"status": "not_found"}
        return {"status": "confirmed", "object_id": object_id,
                "object_type": obj["type"], "seq": self._next_seq()}

    async def commit_semantic(self, team, item_id, content, metadata,
                              proposed_by=None):
        self.semantic_calls.append({"team": team, "item_id": item_id,
                                    "content": content, "metadata": metadata,
                                    "proposed_by": proposed_by})
        return {"object_id": item_id, "seq": self._next_seq()}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stores(monkeypatch):
    admin, pg, falkor = FakeAdminStore(), FakePgStore(), FakeFalkorStore()
    monkeypatch.setattr(State, "admin", admin, raising=False)
    monkeypatch.setattr(State, "pg", pg, raising=False)
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    return admin, pg, falkor


@pytest.fixture
def gate(monkeypatch):
    g = FakeGate()
    monkeypatch.setattr(app.state, "gate", g, raising=False)
    return g


@pytest.fixture
def operator_headers(stores):
    admin, pg, falkor = stores
    op_id = admin.seed_operator(username="alice")
    token = admin.seed_session(operator_id=op_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def team_key_headers(stores):
    from kwim_api import auth as auth_mod
    from kwim_api.keys import generate_key

    full_key, prefix, key_hash = generate_key()
    auth_mod._KEY_MAP[full_key] = "acme"
    return {"Authorization": f"Bearer {full_key}"}


@pytest.fixture
def acme(stores):
    """A team that resolves through _team_path: has a Postgres schema."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    return "acme"


# ---------------------------------------------------------------------------
# auth gating - every mutating route
# ---------------------------------------------------------------------------

WRITE_ROUTES = [
    ("POST", "/v1/admin/teams/acme/facts", {"statement": "s", "fact_type": "product"}),
    ("POST", "/v1/admin/teams/acme/rules", {"situation": {}, "approach": "a"}),
    ("POST", "/v1/admin/teams/acme/semantic", {"content": "c"}),
    ("PATCH", "/v1/admin/teams/acme/facts/f1/amend", {"reason": "r"}),
    ("POST", "/v1/admin/teams/acme/facts/f1/supersede",
     {"statement": "s", "fact_type": "product", "reason": "r"}),
    ("PATCH", "/v1/admin/teams/acme/rules/r1/amend", {"reason": "r"}),
    ("POST", "/v1/admin/teams/acme/rules/r1/supersede", {"approach": "a", "reason": "r"}),
    ("PATCH", "/v1/admin/teams/acme/semantic/s1/amend", {"reason": "r"}),
    ("POST", "/v1/admin/teams/acme/objects/o1/retract",
     {"object_type": "fact", "reason": "r"}),
    ("POST", "/v1/admin/teams/acme/objects/o1/confirm", {"object_type": "fact"}),
    ("POST", "/v1/admin/teams/acme/forget/preview", {"object_ids": ["o1"]}),
    ("POST", "/v1/admin/teams/acme/forget", {"preview_token": "t", "confirm_count": 1}),
    ("POST", "/v1/admin/teams/acme/proposals/p1/approve", {}),
    ("POST", "/v1/admin/teams/acme/proposals/p1/reject", {}),
    ("POST", "/v1/admin/teams/acme/proposals/bulk-reject",
     {"confirm_count": 0, "reason": "r"}),
    ("POST", "/v1/admin/teams/acme/rebuild", {}),
    ("GET", "/v1/admin/jobs/j1", None),
    ("GET", "/v1/admin/jobs", None),
]


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_every_write_route_requires_a_session(client, stores, gate, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_every_write_route_rejects_a_team_key(
    client, stores, gate, team_key_headers, method, path, body,
):
    r = client.request(method, path, json=body, headers=team_key_headers)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_creating_a_fact_commits_human_approved_via_the_gate(
    client, stores, gate, operator_headers, acme,
):
    r = client.post(f"/v1/admin/teams/{acme}/facts",
                    json={"statement": "s", "fact_type": "product"},
                    headers=operator_headers)
    assert r.status_code == 201, r.text
    assert len(gate.commit_calls) == 1
    call = gate.commit_calls[0]
    assert call["gate_decision"] == "human_approved"
    assert call["extra_provenance"] == {"created_by": "alice",
                                        "created_via": "admin_console"}
    admin, pg, falkor = stores
    assert len(admin.audit_rows) == 1
    assert admin.audit_rows[0]["result"] == "ok"


def test_creating_a_constraint_rule_routes_to_review_not_commit(
    client, stores, gate, operator_headers, acme,
):
    r = client.post(f"/v1/admin/teams/{acme}/rules",
                    json={"rule_type": "constraint", "action_pattern": "rm -rf",
                          "verdict": "deny", "authority": "sec",
                          "severity": "high", "check_tier": "deterministic"},
                    headers=operator_headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending_review"
    assert "proposal_id" in body
    assert len(gate.review_calls) == 1
    assert gate.commit_calls == []


def test_creating_an_advisory_rule_commits_directly(
    client, stores, gate, operator_headers, acme,
):
    r = client.post(f"/v1/admin/teams/{acme}/rules",
                    json={"situation": {"task_type": "x"}, "approach": "do y"},
                    headers=operator_headers)
    assert r.status_code == 200, r.text
    assert len(gate.commit_calls) == 1
    assert gate.review_calls == []


# ---------------------------------------------------------------------------
# amend / supersede
# ---------------------------------------------------------------------------

def test_amend_on_a_current_fact_returns_200_and_writes_a_row(
    client, stores, gate, operator_headers, acme,
):
    gate.objects["f1"] = {"type": "fact", "status": "current"}
    r = client.patch(f"/v1/admin/teams/{acme}/facts/f1/amend",
                     json={"statement": "new statement", "reason": "typo fix"},
                     headers=operator_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operation"] == "amend"
    assert len(gate.amend_calls) == 1
    assert gate.amend_calls[0]["reason"] == "typo fix"


def test_amend_on_a_superseded_fact_returns_409_and_writes_no_commit_row(
    client, stores, gate, operator_headers, acme,
):
    gate.objects["f1"] = {"type": "fact", "status": "superseded"}
    before = len(gate.amend_calls)
    r = client.patch(f"/v1/admin/teams/{acme}/facts/f1/amend",
                     json={"statement": "x", "reason": "y"},
                     headers=operator_headers)
    assert r.status_code == 409, r.text
    # Refused: no commit_log row was written.
    assert len(gate.amend_calls) == before + 1
    admin, pg, falkor = stores
    assert admin.audit_rows[-1]["result"] == "denied"


def test_supersede_creates_a_new_object_and_references_the_old_one(
    client, stores, gate, operator_headers, acme,
):
    r = client.post(f"/v1/admin/teams/{acme}/facts/f1/supersede",
                    json={"statement": "new", "fact_type": "product", "reason": "y"},
                    headers=operator_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["supersedes"] == "f1"
    assert gate.commit_calls[0]["body"]["supersedes"] == "f1"


def test_amend_and_supersede_write_audit_rows_carrying_the_reason(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    gate.objects["f1"] = {"type": "fact", "status": "current"}
    client.patch(f"/v1/admin/teams/{acme}/facts/f1/amend",
                json={"statement": "x", "reason": "amend-reason"},
                headers=operator_headers)
    client.post(f"/v1/admin/teams/{acme}/facts/f2/supersede",
               json={"statement": "y", "fact_type": "product",
                     "reason": "supersede-reason"},
               headers=operator_headers)
    reasons = [r["detail"].get("reason") for r in admin.audit_rows]
    assert "amend-reason" in reasons
    assert "supersede-reason" in reasons


def test_amend_missing_reason_is_rejected_before_anything_is_written(
    client, stores, gate, operator_headers, acme,
):
    gate.objects["f1"] = {"type": "fact", "status": "current"}
    r = client.patch(f"/v1/admin/teams/{acme}/facts/f1/amend",
                     json={"statement": "x"}, headers=operator_headers)
    assert r.status_code == 422
    admin, pg, falkor = stores
    assert gate.amend_calls == []
    assert admin.audit_rows == []


# ---------------------------------------------------------------------------
# Forget: preview + execute
# ---------------------------------------------------------------------------

def test_forget_preview_returns_a_plan_and_mutates_nothing(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    falkor.seed_forget_object(acme, id="f1", type="fact", status="current",
                              label="the fact")
    pg.seed_commit(acme, object_id="f1")
    before = list(pg.commit_log[acme])

    r = client.post(f"/v1/admin/teams/{acme}/forget/preview",
                    json={"object_ids": ["f1"]}, headers=operator_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["plan"][0]["id"] == "f1"
    assert pg.commit_log[acme] == before
    assert falkor.forgotten_nodes == []


def test_forget_execute_with_mismatched_confirm_count_deletes_nothing(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    falkor.seed_forget_object(acme, id="f1", type="fact")
    preview = client.post(f"/v1/admin/teams/{acme}/forget/preview",
                          json={"object_ids": ["f1"]},
                          headers=operator_headers).json()

    r = client.post(f"/v1/admin/teams/{acme}/forget",
                    json={"preview_token": preview["preview_token"],
                          "confirm_count": preview["count"] + 1},
                    headers=operator_headers)
    assert r.status_code == 409, r.text
    assert falkor.forgotten_nodes == []


def test_forget_execute_reused_token_returns_409(
    client, stores, gate, operator_headers, acme,
):
    falkor = stores[2]
    falkor.seed_forget_object(acme, id="f1", type="fact")
    preview = client.post(f"/v1/admin/teams/{acme}/forget/preview",
                          json={"object_ids": ["f1"]},
                          headers=operator_headers).json()
    payload = {"preview_token": preview["preview_token"],
              "confirm_count": preview["count"]}

    first = client.post(f"/v1/admin/teams/{acme}/forget", json=payload,
                        headers=operator_headers)
    assert first.status_code == 200, first.text

    second = client.post(f"/v1/admin/teams/{acme}/forget", json=payload,
                         headers=operator_headers)
    assert second.status_code == 409


def test_forget_execute_unknown_or_expired_token_returns_409(
    client, stores, gate, operator_headers, acme,
):
    r = client.post(f"/v1/admin/teams/{acme}/forget",
                    json={"preview_token": "never-issued", "confirm_count": 1},
                    headers=operator_headers)
    assert r.status_code == 409


def test_forget_shared_evidence_guard_keeps_episodics_supporting_another_object(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    falkor.seed_forget_object(acme, id="f1", type="fact", evidence=["ev1"])
    falkor.seed_forget_object(acme, id="f2", type="fact", evidence=["ev1"])
    falkor.seed_supported_by(acme, "ev1", ["f1", "f2"])
    pg.seed_episodic(acme, id="ev1")

    preview = client.post(f"/v1/admin/teams/{acme}/forget/preview",
                          json={"object_ids": ["f1"]},
                          headers=operator_headers).json()
    plan = preview["plan"][0]
    assert plan["episodics_to_delete"] == []
    assert [s["episodic"] for s in plan["episodics_shared"]] == ["ev1"]

    execute = client.post(f"/v1/admin/teams/{acme}/forget",
                          json={"preview_token": preview["preview_token"],
                                "confirm_count": preview["count"]},
                          headers=operator_headers)
    assert execute.status_code == 200, execute.text
    assert execute.json()["shared_skipped"] == [{"episodic": "ev1", "also_supports": ["f2"]}]
    assert [r["id"] for r in pg.episodic[acme]] == ["ev1"]


def test_forget_preview_surfaces_a_failing_delete_preflight_and_blocks_execution(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    pg.seed_preflight(acme, ok=False, role="readonly_role")
    falkor.seed_forget_object(acme, id="f1", type="fact")

    r = client.post(f"/v1/admin/teams/{acme}/forget/preview",
                    json={"object_ids": ["f1"]}, headers=operator_headers)
    assert r.status_code == 409, r.text
    assert "readonly_role" in r.text
    assert falkor._previews == {}


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

def test_approve_commits_with_reviewer_attribution_matching_the_review_router_shape(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    pg.seed_proposal(acme, proposal_id="p1", object_type="fact",
                     body={"statement": "s", "fact_type": "product"})

    r = client.post(f"/v1/admin/teams/{acme}/proposals/p1/approve",
                    headers=operator_headers)
    assert r.status_code == 200, r.text
    call = gate.commit_calls[0]
    assert call["gate_decision"] == "human_approved"
    # Same shape as routers/review.py's approve (extra_provenance keys), only the
    # `_via` value differs from "api"/"mattermost".
    assert set(call["extra_provenance"]) == {"approved_by", "approved_via"}
    assert call["extra_provenance"]["approved_via"] == "admin_console"


def test_approving_an_already_resolved_proposal_returns_409(
    client, stores, gate, operator_headers, acme,
):
    pg = stores[1]
    pg.seed_proposal(acme, proposal_id="p1", resolved_at=_NOW, resolution="approved")

    r = client.post(f"/v1/admin/teams/{acme}/proposals/p1/approve",
                    headers=operator_headers)
    assert r.status_code == 409, r.text
    assert gate.commit_calls == []


def test_resolved_via_admin_console_is_in_the_check_constraint():
    """resolved_via must admit 'admin_console': without it, every
    console resolution fails the CHECK. Reads the schema template directly."""
    import pathlib

    template = pathlib.Path(__file__).parents[3] / "db" / "team-schema.sql.j2"
    assert template.is_file(), f"expected {template}"
    text = template.read_text()
    assert "'admin_console'" in text
    assert "resolved_via" in text


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

def test_triggering_a_rebuild_returns_a_job_id_and_creates_a_running_row(
    client, stores, gate, operator_headers, acme, monkeypatch,
):
    # Don't let the real _run_rebuild background task touch real infra.
    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr("kwim_api.routers.admin_write._run_rebuild", _noop)

    r = client.post(f"/v1/admin/teams/{acme}/rebuild", json={},
                    headers=operator_headers)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    admin = stores[0]
    assert admin.jobs[0]["id"] == job_id
    assert admin.jobs[0]["status"] == "running"
    assert admin.jobs[0]["team"] == acme


def test_a_second_rebuild_trigger_for_the_same_team_returns_409_while_running(
    client, stores, gate, operator_headers, acme, monkeypatch,
):
    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr("kwim_api.routers.admin_write._run_rebuild", _noop)

    first = client.post(f"/v1/admin/teams/{acme}/rebuild", json={},
                        headers=operator_headers)
    assert first.status_code == 202, first.text

    second = client.post(f"/v1/admin/teams/{acme}/rebuild", json={},
                         headers=operator_headers)
    assert second.status_code == 409
    assert first.json()["job_id"] in second.text


async def test_rebuild_uses_its_own_store_connections_not_states(
    stores, monkeypatch,
):
    """Sharing State.pg/State.falkor would serialize every other query in the
    service behind a rebuild that runs for minutes."""
    from kwim_api.routers import admin_write

    admin, real_pg, real_falkor = stores

    class _FakeConn:
        def __init__(self):
            self.connected = False
            self.closed = False

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True

    made: list[_FakeConn] = []

    def _make(*a, **kw):
        c = _FakeConn()
        made.append(c)
        return c

    handed_pg = handed_falkor = None

    async def _fake_rebuild_team(team, pg, falkor, embedder, in_place, yes):
        nonlocal handed_pg, handed_falkor
        handed_pg, handed_falkor = pg, falkor
        return True

    monkeypatch.setattr(admin_write, "PostgresStore", _make)
    monkeypatch.setattr(admin_write, "FalkorStore", _make)
    monkeypatch.setattr(admin_write.rebuild_mod, "rebuild_team", _fake_rebuild_team)

    await admin_write._run_rebuild("job-1", "acme", skip_semantic=True, in_place=False)

    assert handed_pg is not real_pg
    assert handed_falkor is not real_falkor
    assert all(c.connected and c.closed for c in made)
    assert admin.jobs == []  # finish_job was called on the real State.admin fake
    # finish_job updates an existing row, so one is seeded.


async def test_every_running_job_is_marked_failed_at_startup_no_age_threshold(
    monkeypatch,
):
    """A `running` row at startup is orphaned by definition at
    replicas: 1."""
    import kwim_api.main as main_mod

    admin = FakeAdminStore()
    admin.jobs = [
        {"id": "old", "kind": "rebuild", "team": "acme", "status": "running",
         "started_at": _NOW, "finished_at": None, "operator_id": None, "detail": {}},
        {"id": "new", "kind": "rebuild", "team": "beta", "status": "running",
         "started_at": _NOW, "finished_at": None, "operator_id": None, "detail": {}},
        {"id": "done", "kind": "rebuild", "team": "gamma", "status": "succeeded",
         "started_at": _NOW, "finished_at": _NOW, "operator_id": None, "detail": {}},
    ]

    class _Stub:
        async def connect(self):
            pass

        async def close(self):
            pass

    class _StubBus(_Stub):
        _conn = None

        async def channel(self):
            return None

    monkeypatch.setattr(main_mod, "PostgresStore", lambda: _Stub())
    monkeypatch.setattr(main_mod, "FalkorStore", lambda: _Stub())
    monkeypatch.setattr(main_mod, "Embedder", lambda: _Stub())
    monkeypatch.setattr(main_mod, "AdminStore", lambda: admin)

    class _FakeBusConn:
        async def channel(self):
            class _Ch:
                pass
            return _Ch()

    class _FakeBus(_Stub):
        def __init__(self):
            self._conn = _FakeBusConn()

    monkeypatch.setattr(main_mod, "Bus", _FakeBus)

    class _FakeGateCls:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            pass

    class _FakeSemanticConsumer:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            pass

    monkeypatch.setattr(main_mod, "Gate", _FakeGateCls)
    monkeypatch.setattr(main_mod, "SemanticConsumer", _FakeSemanticConsumer)
    monkeypatch.setattr(main_mod, "warn_if_insecure_cookie", lambda: None)

    async with main_mod.lifespan(main_mod.app):
        pass

    statuses = {j["id"]: j["status"] for j in admin.jobs}
    assert statuses["old"] == "failed"
    assert statuses["new"] == "failed"
    assert statuses["done"] == "succeeded"


# ---------------------------------------------------------------------------
# Audit: denial vs error, and no secrets leak into detail
# ---------------------------------------------------------------------------

def test_a_denial_writes_result_denied_and_an_unhandled_error_still_propagates(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores

    r = client.post(f"/v1/admin/teams/{acme}/objects/nope/retract",
                    json={"object_type": "fact", "reason": "x"},
                    headers=operator_headers)
    assert r.status_code == 404
    assert admin.audit_rows[-1]["result"] == "denied"

    gate.raise_on = "amend_object"
    gate.objects["f1"] = {"type": "fact", "status": "current"}
    with pytest.raises(RuntimeError):
        client.patch(f"/v1/admin/teams/{acme}/facts/f1/amend",
                     json={"statement": "x", "reason": "y"},
                     headers=operator_headers)
    assert admin.audit_rows[-1]["result"] == "error"


def test_no_audit_detail_contains_a_secret_or_password_key(
    client, stores, gate, operator_headers, acme,
):
    admin, pg, falkor = stores
    gate.objects["f1"] = {"type": "fact", "status": "current"}
    falkor.seed_forget_object(acme, id="f2", type="fact")

    client.post(f"/v1/admin/teams/{acme}/facts",
               json={"statement": "s", "fact_type": "product"},
               headers=operator_headers)
    client.patch(f"/v1/admin/teams/{acme}/facts/f1/amend",
                json={"statement": "x", "reason": "y"}, headers=operator_headers)
    preview = client.post(f"/v1/admin/teams/{acme}/forget/preview",
                          json={"object_ids": ["f2"]},
                          headers=operator_headers).json()
    client.post(f"/v1/admin/teams/{acme}/forget",
               json={"preview_token": preview["preview_token"],
                     "confirm_count": preview["count"]},
               headers=operator_headers)

    forbidden = ("password", "secret", "key_hash", "token")
    for row in admin.audit_rows:
        for key in row["detail"]:
            assert not any(f in key.lower() for f in forbidden), row


# ---------------------------------------------------------------------------
# Team validation: 422 shape, 404 unknown, both audited
# ---------------------------------------------------------------------------

def test_invalid_team_identifier_is_422_and_audited(
    client, stores, gate, operator_headers,
):
    admin = stores[0]
    r = client.post("/v1/admin/teams/DROP TABLE;/facts",
                    json={"statement": "s", "fact_type": "x"},
                    headers=operator_headers)
    assert r.status_code == 422
    assert gate.commit_calls == []
    assert admin.audit_rows[-1]["result"] == "denied"


def test_unknown_team_is_404(client, stores, gate, operator_headers):
    r = client.post("/v1/admin/teams/nosuchteam/facts",
                    json={"statement": "s", "fact_type": "x"},
                    headers=operator_headers)
    assert r.status_code == 404
    assert gate.commit_calls == []


def test_malformed_older_than_is_audited(client, stores, operator_headers):
    """An authenticated operator's denial gets an audit row, whatever denied it."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")

    r = client.post("/v1/admin/teams/acme/proposals/bulk-reject",
                    json={"confirm_count": 0, "reason": "x",
                          "older_than": "not a timestamp"},
                    headers=operator_headers)
    assert r.status_code == 422

    denied = [a for a in admin.audit_rows
              if a["action"] == "proposal.bulk_reject" and a["result"] == "denied"]
    assert denied, "a malformed bulk-reject left no audit row"


def test_forget_execute_detects_drift_since_the_preview(client, stores, operator_headers):
    """The count gate exists to abort when the live set drifted between
    planning and executing."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(3):
        falkor.seed_forget_object("acme", id=f"g{i}", type="fact", label="junk")

    prev = client.post("/v1/admin/teams/acme/forget/preview",
                       json={"select": {"type": "fact", "statement_contains": "junk"}},
                       headers=operator_headers).json()
    assert prev["count"] == 3

    # A fourth matching object lands before the operator confirms.
    falkor.seed_forget_object("acme", id="g3", type="fact", label="junk")

    r = client.post("/v1/admin/teams/acme/forget",
                    json={"preview_token": prev["preview_token"], "confirm_count": 3},
                    headers=operator_headers)
    assert r.status_code == 409, (
        "the live set changed since the preview; executing the stale plan is what "
        "the count gate is supposed to prevent")


def test_forget_execute_rechecks_the_shared_evidence_guard(client, stores, operator_headers):
    """The guard's verdict is computed per plan. Freezing it at preview time means
    an episodic that gained a dependant would be deleted."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    falkor.seed_forget_object("acme", id="f1", type="fact", evidence=["ev1"])
    falkor.seed_supported_by("acme", "ev1", ["f1"])

    prev = client.post("/v1/admin/teams/acme/forget/preview",
                       json={"object_ids": ["f1"]}, headers=operator_headers).json()
    assert prev["plan"][0]["episodics_to_delete"] == ["ev1"]

    # Another live object comes to depend on the same episodic.
    falkor.seed_forget_object("acme", id="f2", type="fact", evidence=["ev1"])
    falkor.seed_supported_by("acme", "ev1", ["f1", "f2"])

    client.post("/v1/admin/teams/acme/forget",
                json={"preview_token": prev["preview_token"], "confirm_count": 1},
                headers=operator_headers)
    assert "ev1" not in pg.deleted_episodic_ids, (
        "deleted an episodic that now supports a live object")


@pytest.mark.parametrize("path,method,payload,offender", [
    ("/v1/admin/teams/acme/facts/f1/amend", "patch",
     {"fact_type": "t", "status": "retracted", "reason": "r"}, "status"),
    ("/v1/admin/teams/acme/facts/f1/supersede", "post",
     {"statement": "s", "fact_type": "t", "abuot": ["x"], "reason": "r"}, "abuot"),
    ("/v1/admin/teams/acme/proposals/bulk-reject", "post",
     {"source_knid": "repo_sync", "confirm_count": 3, "reason": "r"}, "source_knid"),
])
def test_unknown_field_in_a_mutation_body_is_422(client, stores, operator_headers,
                                                 path, method, payload, offender):
    """An unknown field in a console request body is a 422."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    falkor.seed_fact("acme", id="f1")

    r = getattr(client, method)(path, json=payload, headers=operator_headers)
    assert r.status_code == 422
    assert offender in r.text, "the rejection must name the field"
    assert pg.commit_log.get("acme", []) == [], "nothing was written"


def test_agent_proposal_models_still_accept_extra_fields():
    """The tightening stops at the console. /v1/wisdom/propose serves agent clients
    outside this repo."""
    from kwim_api.models import AdvisoryProposal, FactProposal

    FactProposal(statement="s", fact_type="t", unknown_key="x")
    AdvisoryProposal(situation={}, approach="a", unknown_key="x")
