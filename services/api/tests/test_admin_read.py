"""Tests for the /v1/admin cross-team read API.

Router tests against fakes on `runtime.State`: auth gating, team-path validation,
query parameters, the list envelope, and cursor round trips.
"""
import datetime
import uuid

import pytest

from kwim_api.config import settings
from kwim_api.keys import generate_key
from kwim_api.runtime import State

pytestmark = pytest.mark.usefixtures("admin_enabled")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeAdminStore:
    """Covers both current_operator's session resolution and the new admin-read
    console methods (teams/keys/operators/audit)."""

    def __init__(self):
        self.operators: dict[str, dict] = {}
        self.operators_by_id: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.teams: dict[str, dict] = {}
        self.api_keys: list[dict] = []
        self.audit_rows: list[dict] = []
        self.calls: list[tuple] = []
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    # --- auth (mirrors test_admin_identity.FakeAdminStore) ---

    def seed_operator(self, *, username: str, is_active: bool = True) -> str:
        op_id = self._new_id("op")
        row = {"id": op_id, "username": username, "is_active": is_active}
        self.operators[username] = row
        self.operators_by_id[op_id] = row
        return op_id

    def seed_session(self, *, operator_id: str, expires_at=None) -> str:
        from kwim_api.admin_auth import hash_token

        token = f"session-{self._new_id('tok')}"
        expires_at = expires_at or (datetime.datetime.now(datetime.UTC)
                                    + datetime.timedelta(hours=1))
        self.sessions[hash_token(token)] = {
            "id": self._new_id("sess"), "operator_id": operator_id,
            "created_at": datetime.datetime.now(datetime.UTC),
            "expires_at": expires_at, "revoked_at": None,
        }
        return token

    async def get_session(self, token_hash):
        row = self.sessions.get(token_hash)
        if row is None or row["revoked_at"] is not None:
            return None
        if row["expires_at"] <= datetime.datetime.now(datetime.UTC):
            return None
        op = self.operators_by_id.get(row["operator_id"])
        if op is None or not op["is_active"]:
            return None
        return {"id": row["id"], "operator_id": row["operator_id"], "username": op["username"],
               "created_at": row["created_at"], "expires_at": row["expires_at"]}

    async def refresh_session(self, token_hash, expires_at):
        if token_hash in self.sessions:
            self.sessions[token_hash]["expires_at"] = expires_at

    async def record(self, **kwargs):
        pass

    # --- team registry ---

    def seed_team(self, team: str, **kw):
        self.teams[team] = {"team": team, "display_name": kw.get("display_name"),
                            "status": kw.get("status", "active"),
                            "created_at": kw.get("created_at", _NOW), "created_by": None,
                            "decommissioned_at": None}

    async def list_teams(self):
        self.calls.append(("list_teams",))
        return list(self.teams.values())

    async def get_team(self, team):
        self.calls.append(("get_team", team))
        return self.teams.get(team)

    # --- keys / operators / audit ---

    def seed_key(self, **kw):
        self.api_keys.append(kw)

    async def list_api_keys(self, *, team=None, include_revoked=False):
        self.calls.append(("list_api_keys", team, include_revoked))
        rows = self.api_keys
        if team is not None:
            rows = [r for r in rows if r["team"] == team]
        if not include_revoked:
            rows = [r for r in rows if r.get("revoked_at") is None]
        # As AdminStore.list_api_keys: no key_hash.
        return [{k: v for k, v in r.items() if k != "key_hash"} for r in rows]

    async def list_operators(self):
        self.calls.append(("list_operators",))
        return [{"id": r["id"], "username": r["username"], "display_name": None,
                "is_active": r["is_active"], "created_at": _NOW, "last_login_at": None}
                for r in self.operators.values()]

    def seed_audit(self, **kw):
        row = {"seq": len(self.audit_rows) + 1, "at": kw.get("at", _NOW),
              "operator_id": kw.get("operator_id"), "action": kw.get("action", "x"),
              "team": kw.get("team"), "object_type": None, "object_id": None,
              "detail": {}, "result": kw.get("result", "ok")}
        self.audit_rows.append(row)

    async def list_audit(self, *, team=None, operator_id=None, action=None, since=None,
                         cursor=None, limit=100):
        self.calls.append(("list_audit", team, operator_id, action, since, cursor, limit))
        rows = self.audit_rows
        if team is not None:
            rows = [r for r in rows if r["team"] == team]
        if operator_id is not None:
            rows = [r for r in rows if r["operator_id"] == operator_id]
        if action is not None:
            rows = [r for r in rows if r["action"] == action]
        if since is not None:
            rows = [r for r in rows if r["at"] >= since]
        rows = sorted(rows, key=lambda r: r["seq"], reverse=True)
        if cursor is not None:
            rows = [r for r in rows if r["seq"] < cursor]
        return rows[:limit]


_NOW = datetime.datetime(2026, 6, 11, 12, 0, 0, tzinfo=datetime.UTC)


class FakePgStore:
    def __init__(self):
        self.schemas: set[str] = set()
        self.episodic: dict[str, list[dict]] = {}
        self.commit_log: dict[str, list[dict]] = {}
        self.proposals: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self._commit_seq = 0

    def seed_schema(self, team: str):
        self.schemas.add(team)

    async def list_team_schemas(self):
        self.calls.append(("list_team_schemas",))
        return sorted(self.schemas)

    # --- episodic ---

    def seed_episodic(self, team, *, archived=False, **kw):
        row = {"id": kw.get("id", str(uuid.uuid4())), "agent_id": kw.get("agent_id", "a1"),
              "session_id": kw.get("session_id", "s1"), "event_type": kw.get("event_type", "obs"),
              "event_data": kw.get("event_data", {}),
              "occurred_at": kw.get("occurred_at", _NOW), "archived": archived}
        self.episodic.setdefault(team, []).append(row)

    async def read_episodic(self, team, since_ts=None, since_id=None, limit=500,
                            event_type=None, agent_id=None, order="asc", archived="false"):
        self.calls.append(("read_episodic", team, archived))
        rows = self.episodic.get(team, [])
        if archived == "false":
            rows = [r for r in rows if not r["archived"]]
        elif archived == "true":
            rows = [r for r in rows if r["archived"]]
        if event_type is not None:
            rows = [r for r in rows if r["event_type"] == event_type]
        if agent_id is not None:
            rows = [r for r in rows if r["agent_id"] == agent_id]
        rows = sorted(rows, key=lambda r: (r["occurred_at"], r["id"]), reverse=(order == "desc"))
        # As the store: (occurred_at, id) is exclusive, lower bound ascending and
        # upper bound descending.
        if since_ts is not None and since_id is not None:
            key = (since_ts, str(since_id))
            if order == "desc":
                rows = [r for r in rows if (r["occurred_at"], str(r["id"])) < key]
            else:
                rows = [r for r in rows if (r["occurred_at"], str(r["id"])) > key]
        return rows[:limit]

    async def count_episodic(self, team):
        self.calls.append(("count_episodic", team))
        return len([r for r in self.episodic.get(team, []) if not r["archived"]])

    async def pending_stats(self, team, source_kind=None):
        rows = [p for p in self.proposals.get(team, []) if p["resolved_at"] is None]
        return {"count": len(rows), "distinct": len(rows), "sample": []}

    # --- commit log ---

    def seed_commit(self, team, **kw):
        self._commit_seq += 1
        row = {"seq": self._commit_seq, "committed_at": kw.get("committed_at", _NOW),
              "object_type": kw.get("object_type", "fact"), "object_id": kw.get("object_id", "o1"),
              "operation": kw.get("operation", "commit"), "payload": kw.get("payload", {}),
              "provenance": kw.get("provenance", {}), "proposed_by": kw.get("proposed_by"),
              "source_kind": kw.get("source_kind", "agent_proposal"),
              "gate_decision": kw.get("gate_decision", "auto_committed")}
        self.commit_log.setdefault(team, []).append(row)
        return row["seq"]

    def _filter_commit(self, team, filters):
        rows = self.commit_log.get(team, [])
        for key in ("object_id", "object_type", "operation", "source_kind", "gate_decision"):
            val = (filters or {}).get(key)
            if val is not None:
                rows = [r for r in rows if r[key] == val]
        return rows

    async def read_commit_log(self, team, *, filters=None, since_seq=None, order="asc", limit=100):
        self.calls.append(("read_commit_log", team, filters, since_seq, order, limit))
        rows = self._filter_commit(team, filters)
        rows = sorted(rows, key=lambda r: r["seq"], reverse=(order == "desc"))
        if since_seq is not None:
            if order == "desc":
                rows = [r for r in rows if r["seq"] < since_seq]
            else:
                rows = [r for r in rows if r["seq"] > since_seq]
        return rows[:limit]

    async def count_commit_log(self, team, *, filters=None):
        self.calls.append(("count_commit_log", team, filters))
        return len(self._filter_commit(team, filters))

    # --- proposals ---

    def seed_proposal(self, team, **kw):
        row = {"proposal_id": kw.get("proposal_id", str(uuid.uuid4())),
              "object_type": kw.get("object_type", "fact"),
              "proposed_by": kw.get("proposed_by", "agent-1"),
              "body": kw.get("body", {"statement": "x"}),
              "bus_message": {}, "created_at": kw.get("created_at", _NOW),
              "resolved_at": kw.get("resolved_at"), "resolution": kw.get("resolution"),
              "resolved_by": kw.get("resolved_by"), "resolved_via": kw.get("resolved_via"),
              "reject_reason": kw.get("reject_reason")}
        self.proposals.setdefault(team, []).append(row)
        return row

    def _filter_proposals(self, team, resolved, resolution, object_type, source_kind=None):
        rows = self.proposals.get(team, [])
        if resolved == "false":
            rows = [r for r in rows if r["resolved_at"] is None]
        elif resolved == "true":
            rows = [r for r in rows if r["resolved_at"] is not None]
        if resolution is not None:
            rows = [r for r in rows if r["resolution"] == resolution]
        if object_type is not None:
            rows = [r for r in rows if r["object_type"] == object_type]
        if source_kind is not None:
            # Mirrors the store's body->>'source_kind', not a top-level column.
            rows = [r for r in rows
                    if (r.get("body") or {}).get("source_kind") == source_kind]
        return rows

    async def list_proposals(self, team, *, resolved="false", resolution=None, object_type=None,
                             source_kind=None, cursor=None, limit=50):
        self.calls.append(("list_proposals", team, resolved, resolution, object_type, cursor, limit))
        rows = self._filter_proposals(team, resolved, resolution, object_type, source_kind)
        rows = sorted(rows, key=lambda r: (r["created_at"], r["proposal_id"]), reverse=True)
        if cursor is not None:
            ck, cid = cursor["created_at"], cursor["id"]
            rows = [r for r in rows if (r["created_at"], r["proposal_id"]) < (ck, cid)]
        return rows[:limit]

    async def count_proposals(self, team, *, resolved="false", resolution=None, object_type=None,
                              source_kind=None):
        self.calls.append(("count_proposals", team, resolved, resolution, object_type))
        return len(self._filter_proposals(team, resolved, resolution, object_type, source_kind))


class FakeFalkorStore:
    def __init__(self):
        self.facts: dict[str, list[dict]] = {}
        self.rules: dict[str, list[dict]] = {}
        self.semantic: dict[str, list[dict]] = {}
        self.working: dict[tuple, dict] = {}
        self.repos: dict[str, set] = {}
        self.calls: list[tuple] = []

    # --- facts ---

    def seed_fact(self, team, **kw):
        row = {"id": kw["id"], "statement": kw.get("statement", "s"),
              "fact_type": kw.get("fact_type", "product"), "status": kw.get("status", "current"),
              "created_at": kw.get("created_at", "1750000000000"), "about": kw.get("about", []),
              "decay_class": kw.get("decay_class", "slow"),
              "source_kind": kw.get("source_kind", "agent_proposal"),
              "last_verified_at": kw.get("last_verified_at"), "commit_seq": kw.get("commit_seq", 0)}
        self.facts.setdefault(team, []).append(row)
        return row

    def _filter_facts(self, team, *, status, fact_type, source_kind, about, q):
        rows = self.facts.get(team, [])
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        if fact_type is not None:
            rows = [r for r in rows if r["fact_type"] == fact_type]
        if source_kind is not None:
            rows = [r for r in rows if r["source_kind"] == source_kind]
        if about:
            rows = [r for r in rows
                    if any(a.lower() in [x.lower() for x in r["about"]] for a in about)]
        if q is not None:
            rows = [r for r in rows if q.lower() in r["statement"].lower()]
        return rows

    async def query_facts_admin(self, team, *, status=None, fact_type=None, source_kind=None,
                                about=None, q=None, cursor=None, limit=50):
        self.calls.append(("query_facts_admin", team, status, fact_type, source_kind, about, q,
                          cursor, limit))
        rows = self._filter_facts(team, status=status, fact_type=fact_type,
                                  source_kind=source_kind, about=about, q=q)
        rows = sorted(rows, key=lambda r: (r["commit_seq"] or 0, r["id"]))
        if cursor is not None:
            cs, cid = cursor["seq"], cursor["id"]
            rows = [r for r in rows if (r["commit_seq"] or 0, r["id"]) > (cs, cid)]
        return rows[:limit]

    async def count_facts_admin(self, team, *, status=None, fact_type=None, source_kind=None,
                                about=None, q=None):
        self.calls.append(("count_facts_admin", team, status, fact_type, source_kind, about, q))
        return len(self._filter_facts(team, status=status, fact_type=fact_type,
                                      source_kind=source_kind, about=about, q=q))

    def seed_fact_provenance(self, team, fact_id, row):
        self._fact_provenance = getattr(self, "_fact_provenance", {})
        self._fact_provenance[(team, fact_id)] = row

    async def get_fact_provenance(self, team, fact_id):
        return getattr(self, "_fact_provenance", {}).get((team, fact_id))

    async def audit_fact(self, team, fact_id):
        return getattr(self, "_fact_provenance", {}).get((team, fact_id)) and []

    # --- rules ---

    def seed_rule(self, team, **kw):
        row = {"id": kw["id"], "rule_type": kw.get("rule_type", "advisory"),
              "situation": kw.get("situation"), "approach": kw.get("approach"),
              "evidence_count": kw.get("evidence_count", 0), "status": kw.get("status", "approved"),
              "scope": kw.get("scope", "team"), "action_pattern": kw.get("action_pattern"),
              "verdict": kw.get("verdict"), "authority": kw.get("authority"),
              "severity": kw.get("severity"), "check_tier": kw.get("check_tier"),
              "promoted_from_id": None, "commit_seq": kw.get("commit_seq", 0)}
        self.rules.setdefault(team, []).append(row)
        return row

    def _filter_rules(self, team, *, status, rule_type, scope):
        rows = self.rules.get(team, [])
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        if rule_type is not None:
            rows = [r for r in rows if r["rule_type"] == rule_type]
        if scope is not None:
            rows = [r for r in rows if r["scope"] == scope]
        return rows

    async def query_rules_admin(self, team, *, status=None, rule_type=None, scope=None,
                                cursor=None, limit=50):
        self.calls.append(("query_rules_admin", team, status, rule_type, scope, cursor, limit))
        rows = self._filter_rules(team, status=status, rule_type=rule_type, scope=scope)
        rows = sorted(rows, key=lambda r: (r["commit_seq"] or 0, r["id"]))
        if cursor is not None:
            cs, cid = cursor["seq"], cursor["id"]
            rows = [r for r in rows if (r["commit_seq"] or 0, r["id"]) > (cs, cid)]
        return rows[:limit]

    async def count_rules_admin(self, team, *, status=None, rule_type=None, scope=None):
        self.calls.append(("count_rules_admin", team, status, rule_type, scope))
        return len(self._filter_rules(team, status=status, rule_type=rule_type, scope=scope))

    async def get_rule_provenance(self, team, rule_id):
        for r in self.rules.get(team, []):
            if r["id"] == rule_id:
                return {**r, "proposed_by": None, "learned_from": [], "promoted_from_team": None}
        return None

    # --- semantic ---

    def seed_semantic(self, team, **kw):
        row = {"id": kw["id"], "content": kw.get("content", "c"),
              "metadata": kw.get("metadata", {}), "created_at": kw.get("created_at", 0)}
        self.semantic.setdefault(team, []).append(row)
        return row

    async def get_by_metadata(self, team, filters):
        if not filters:
            return []
        rows = self.semantic.get(team, [])
        return [r for r in rows if all(r["metadata"].get(k) == v for k, v in filters.items())]

    async def list_semantic(self, team, *, filters=None, cursor=None, limit=50):
        self.calls.append(("list_semantic", team, filters, cursor, limit))
        rows = self.semantic.get(team, [])
        for k, v in (filters or {}).items():
            rows = [r for r in rows if r["metadata"].get(k) == v]
        rows = sorted(rows, key=lambda r: (r["created_at"], r["id"]))
        if cursor is not None:
            cc, cid = cursor["created_at"], cursor["id"]
            rows = [r for r in rows if (r["created_at"], r["id"]) > (cc, cid)]
        return rows[:limit]

    async def count_semantic(self, team, *, filters=None):
        self.calls.append(("count_semantic", team, filters))
        rows = self.semantic.get(team, [])
        for k, v in (filters or {}).items():
            rows = [r for r in rows if r["metadata"].get(k) == v]
        return len(rows)

    async def query_semantic(self, team, qvec, limit, filters):
        rows = await self.list_semantic(team, filters=filters, limit=limit)
        return [{**{k: v for k, v in r.items() if k != "created_at"}, "score": 0.1} for r in rows]

    # --- working ---

    def seed_working(self, team, session, key, value, ttl_seconds=None):
        self.working[(team, session, key)] = ttl_seconds

    async def working_list(self, team, session):
        return [{"session": session, "key": k, "ttl_seconds": ttl}
                for (t, s, k), ttl in self.working.items() if t == team and s == session]

    # --- code graph ---

    def seed_repo(self, team, repo):
        self.repos.setdefault(team, set()).add(repo)

    async def code_indexed_repos(self, team):
        return self.repos.get(team, set())

    async def code_search(self, team, *, qvec=None, name=None, repos=None, limit=10):
        return []

    async def code_trace_calls(self, team, *, fn_id, direction="outbound", depth=2,
                               min_confidence=0.0):
        return []

    async def code_architecture(self, team, *, repos=None):
        return {"communities": []}

    async def code_changed_since(self, team, *, commit, repo):
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_enabled():
    object.__setattr__(settings, "admin_enabled", True)
    try:
        yield
    finally:
        object.__setattr__(settings, "admin_enabled", False)


@pytest.fixture
def stores(monkeypatch):
    admin, pg, falkor = FakeAdminStore(), FakePgStore(), FakeFalkorStore()
    monkeypatch.setattr(State, "admin", admin, raising=False)
    monkeypatch.setattr(State, "pg", pg, raising=False)
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    return admin, pg, falkor


@pytest.fixture
def operator_headers(stores):
    admin, pg, falkor = stores
    op_id = admin.seed_operator(username="alice")
    token = admin.seed_session(operator_id=op_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def team_key_headers(stores):
    full_key, prefix, key_hash = generate_key()
    import kwim_api.auth as auth_mod
    auth_mod._KEY_MAP[full_key] = "acme"
    return {"Authorization": f"Bearer {full_key}"}


ADMIN_ROUTES = [
    ("GET", "/v1/admin/teams"),
    ("GET", "/v1/admin/teams/acme"),
    ("GET", "/v1/admin/teams/acme/facts"),
    ("GET", "/v1/admin/teams/acme/facts/f1"),
    ("GET", "/v1/admin/teams/acme/rules"),
    ("GET", "/v1/admin/teams/acme/rules/r1"),
    ("GET", "/v1/admin/teams/acme/semantic"),
    ("GET", "/v1/admin/teams/acme/episodic"),
    ("GET", "/v1/admin/teams/acme/working?session=s1"),
    ("GET", "/v1/admin/teams/acme/commit-log"),
    ("GET", "/v1/admin/teams/acme/proposals"),
    ("GET", "/v1/admin/teams/acme/code/repos"),
    ("GET", "/v1/admin/teams/acme/code/search"),
    ("GET", "/v1/admin/teams/acme/code/functions/fn1/trace"),
    ("GET", "/v1/admin/teams/acme/code/architecture"),
    ("GET", "/v1/admin/teams/acme/code/changes?repo=r&commit=c"),
    ("GET", "/v1/admin/keys"),
    ("GET", "/v1/admin/operators"),
    ("GET", "/v1/admin/audit"),
]


# ---------------------------------------------------------------------------
# auth gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_every_admin_read_route_requires_a_session(client, stores, method, path):
    r = client.request(method, path)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_every_admin_read_route_rejects_a_team_key(client, stores, team_key_headers, method, path):
    r = client.request(method, path, headers=team_key_headers)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# GET /teams reconciliation
# ---------------------------------------------------------------------------

def test_teams_reconciles_schema_only_and_console_only_rows(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("schema-only")             # provisioned outside the console
    admin.seed_team("console-only")           # console record, schema never created

    body = client.get("/v1/admin/teams", headers=operator_headers).json()
    by_team = {i["team"]: i for i in body["items"]}

    assert by_team["schema-only"]["has_schema"] is True
    assert by_team["schema-only"]["has_console_record"] is False
    assert by_team["console-only"]["has_schema"] is False
    assert by_team["console-only"]["has_console_record"] is True


# ---------------------------------------------------------------------------
# facts default to status=any
# ---------------------------------------------------------------------------

def test_facts_default_to_status_any_and_include_superseded_and_retracted(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    falkor.seed_fact("acme", id="f1", status="current", commit_seq=1)
    falkor.seed_fact("acme", id="f2", status="superseded", commit_seq=2)
    falkor.seed_fact("acme", id="f3", status="retracted", commit_seq=3)

    body = client.get("/v1/admin/teams/acme/facts", headers=operator_headers).json()
    assert {i["id"] for i in body["items"]} == {"f1", "f2", "f3"}
    assert falkor.calls[-1][2] is None            # status=None reached the store


# ---------------------------------------------------------------------------
# rules return pending/deprecated/retracted
# ---------------------------------------------------------------------------

def test_rules_return_every_status_not_just_approved(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i, status_ in enumerate(("pending", "approved", "deprecated", "retracted")):
        falkor.seed_rule("acme", id=f"r{i}", status=status_, commit_seq=i)

    body = client.get("/v1/admin/teams/acme/rules", headers=operator_headers).json()
    assert {i["status"] for i in body["items"]} == {"pending", "approved", "deprecated", "retracted"}


# ---------------------------------------------------------------------------
# semantic listing with no filters (get_by_metadata returns [] for none)
# ---------------------------------------------------------------------------

def test_semantic_listing_with_no_filters_returns_everything(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    falkor.seed_semantic("acme", id="s1", created_at=1)
    falkor.seed_semantic("acme", id="s2", created_at=2)

    body = client.get("/v1/admin/teams/acme/semantic", headers=operator_headers).json()
    assert {i["id"] for i in body["items"]} == {"s1", "s2"}


# ---------------------------------------------------------------------------
# commit-log filters compose, since_seq paginates without gaps/repeats
# ---------------------------------------------------------------------------

def test_commit_log_filters_compose(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(6):
        pg.seed_commit("acme", object_type="fact" if i % 2 == 0 else "rule",
                       source_kind="agent_proposal")

    body = client.get("/v1/admin/teams/acme/commit-log?object_type=fact",
                      headers=operator_headers).json()
    assert len(body["items"]) == 3
    assert all(i["object_type"] == "fact" for i in body["items"])


def test_commit_log_since_seq_paginates_without_gaps_or_repeats(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    seqs = [pg.seed_commit("acme") for _ in range(25)]

    seen = []
    since = None
    for _ in range(10):
        url = "/v1/admin/teams/acme/commit-log?limit=10" + (f"&since_seq={since}" if since else "")
        page = client.get(url, headers=operator_headers).json()
        if not page["items"]:
            break
        seen.extend(i["seq"] for i in page["items"])
        since = page["items"][-1]["seq"]
    assert sorted(seen) == sorted(seqs)
    assert len(seen) == len(set(seen))


# ---------------------------------------------------------------------------
# proposals resolved=true includes reject_reason
# ---------------------------------------------------------------------------

def test_proposals_resolved_true_returns_rejected_rows_with_reason(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    pg.seed_proposal("acme", resolved_at=_NOW, resolution="rejected",
                     reject_reason="duplicate of f1")
    pg.seed_proposal("acme")   # unresolved

    body = client.get("/v1/admin/teams/acme/proposals?resolved=true",
                      headers=operator_headers).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["resolution"] == "rejected"
    assert body["items"][0]["reject_reason"] == "duplicate of f1"


# ---------------------------------------------------------------------------
# cursor round-trip over 25 rows at limit=10
# ---------------------------------------------------------------------------

def test_facts_cursor_round_trip_covers_every_row_exactly_once(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(25):
        falkor.seed_fact("acme", id=f"f{i:02d}", commit_seq=i)

    seen, pages = [], 0
    cursor = None
    while True:
        url = "/v1/admin/teams/acme/facts?limit=10" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=operator_headers).json()
        seen.extend(i["id"] for i in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert sorted(seen) == [f"f{i:02d}" for i in range(25)]
    assert len(seen) == len(set(seen))
    assert pages == 3, "25 rows at limit=10 is three pages, not four"


def test_a_short_page_ends_the_walk(client, stores, operator_headers):
    """A page shorter than the limit has no successor, so it must not hand out a
    cursor."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(3):
        falkor.seed_fact("acme", id=f"f{i:02d}", commit_seq=i)

    page = client.get("/v1/admin/teams/acme/facts?limit=10",
                      headers=operator_headers).json()
    assert len(page["items"]) == 3
    assert page["next_cursor"] is None


def test_an_exactly_full_page_still_offers_a_cursor(client, stores, operator_headers):
    """The boundary the short-page rule must not overshoot: a full page might have
    a successor, so it gets a cursor even when the next page is empty."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(10):
        falkor.seed_fact("acme", id=f"f{i:02d}", commit_seq=i)

    page = client.get("/v1/admin/teams/acme/facts?limit=10",
                      headers=operator_headers).json()
    assert len(page["items"]) == 10
    assert page["next_cursor"] is not None

    nxt = client.get(f"/v1/admin/teams/acme/facts?limit=10&cursor={page['next_cursor']}",
                     headers=operator_headers).json()
    assert nxt["items"] == []
    assert nxt["next_cursor"] is None


# ---------------------------------------------------------------------------
# malformed cursor -> 422
# ---------------------------------------------------------------------------

def test_malformed_cursor_returns_422(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    r = client.get("/v1/admin/teams/acme/facts?cursor=not-valid-base64!!!",
                   headers=operator_headers)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# invalid team identifier -> 422 before any store call (injection guard)
# ---------------------------------------------------------------------------

def test_invalid_team_identifier_is_422_before_any_store_call(client, stores, operator_headers):
    admin, pg, falkor = stores
    r = client.get("/v1/admin/teams/DROP TABLE;/facts", headers=operator_headers)
    assert r.status_code == 422
    assert falkor.calls == []
    assert pg.calls == []


# ---------------------------------------------------------------------------
# unknown but valid team -> 404
# ---------------------------------------------------------------------------

def test_unknown_valid_team_is_404(client, stores, operator_headers):
    r = client.get("/v1/admin/teams/nosuchteam/facts", headers=operator_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# ?count=true populates total; omitted leaves it null and issues no count query
# ---------------------------------------------------------------------------

def test_count_true_populates_total_and_omitted_skips_the_count_query(
    client, stores, operator_headers,
):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    falkor.seed_fact("acme", id="f1", commit_seq=1)

    r1 = client.get("/v1/admin/teams/acme/facts", headers=operator_headers).json()
    assert r1["total"] is None
    assert not any(c[0] == "count_facts_admin" for c in falkor.calls)

    falkor.calls.clear()
    r2 = client.get("/v1/admin/teams/acme/facts?count=true", headers=operator_headers).json()
    assert r2["total"] == 1
    assert sum(1 for c in falkor.calls if c[0] == "count_facts_admin") == 1


# ---------------------------------------------------------------------------
# no secret ever leaks
# ---------------------------------------------------------------------------

def test_keys_route_never_returns_key_hash_or_secret(client, stores, operator_headers):
    admin, pg, falkor = stores
    admin.seed_key(id="k1", team="acme", label="l", key_prefix="abc123",
                  key_hash="should-never-appear", capabilities=[], created_at=_NOW,
                  created_by=None, expires_at=None, revoked_at=None, last_used_at=None)

    body = client.get("/v1/admin/keys", headers=operator_headers).text
    assert "should-never-appear" not in body
    assert "key_hash" not in body


def test_no_admin_route_response_leaks_password_hash(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    admin.seed_operator(username="bob")

    for method, path in ADMIN_ROUTES:
        r = client.request(method, path, headers=operator_headers)
        assert "password_hash" not in r.text, f"{method} {path} leaked password_hash"


# ---------------------------------------------------------------------------
# Every endpoint accepts the cursor it returns
# ---------------------------------------------------------------------------

def test_episodic_accepts_the_cursor_it_returns(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for _ in range(6):
        pg.seed_episodic("acme", event_type="turn")

    first = client.get("/v1/admin/teams/acme/episodic?limit=3",
                       headers=operator_headers).json()
    assert len(first["items"]) == 3
    assert first["next_cursor"] is not None

    second = client.get(
        f"/v1/admin/teams/acme/episodic?limit=3&cursor={first['next_cursor']}",
        headers=operator_headers)
    assert second.status_code == 200
    ids = {i["id"] for i in first["items"]} & {i["id"] for i in second.json()["items"]}
    assert not ids, "the second page repeated rows from the first"


def test_commit_log_accepts_the_cursor_it_returns(client, stores, operator_headers):
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(6):
        pg.seed_commit("acme", object_id=f"f{i}", operation="commit")

    first = client.get("/v1/admin/teams/acme/commit-log?limit=3",
                       headers=operator_headers).json()
    assert len(first["items"]) == 3
    assert first["next_cursor"] is not None

    second = client.get(
        f"/v1/admin/teams/acme/commit-log?limit=3&cursor={first['next_cursor']}",
        headers=operator_headers)
    assert second.status_code == 200
    seqs = {i["seq"] for i in first["items"]} & {i["seq"] for i in second.json()["items"]}
    assert not seqs, "the second page repeated rows from the first"


def test_cursor_and_raw_offset_together_is_422(client, stores, operator_headers):
    """Two ways to say where to start is a contradiction, not a merge."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    cur = client.get("/v1/admin/teams/acme/commit-log?limit=1",
                     headers=operator_headers).json()["next_cursor"]
    r = client.get(f"/v1/admin/teams/acme/commit-log?since_seq=1&cursor={cur or 'x'}",
                   headers=operator_headers)
    assert r.status_code == 422


def test_proposals_filter_by_source_kind_for_bulk_reject(client, stores, operator_headers):
    """bulk-reject acts on a source_kind set and demands a typed count. Without this
    filter, the console could not show what a bulk rejection would resolve."""
    admin, pg, falkor = stores
    pg.seed_schema("acme")
    for i in range(3):
        pg.seed_proposal("acme", proposal_id=f"p{i}", body={"source_kind": "repo_sync"})
    pg.seed_proposal("acme", proposal_id="p9", body={"source_kind": "agent_proposal"})

    r = client.get("/v1/admin/teams/acme/proposals?source_kind=repo_sync&count=true",
                   headers=operator_headers).json()
    assert r["total"] == 3
    assert {i["proposal_id"] for i in r["items"]} == {"p0", "p1", "p2"}

    unfiltered = client.get("/v1/admin/teams/acme/proposals?count=true",
                            headers=operator_headers).json()
    assert unfiltered["total"] == 4
