"""Semantic search over governed facts - the K-side Tier 1 retrieval path.

Covers the four pieces that make a free-text subject retrievable:
  - FalkorStore.search_facts     - index KNN unfiltered, exact scan when filtered
                                   (no top-k cliff), full fact projection + score
  - GET /v1/knowledge/search     - distance ranking preserved, 503 (never []) when
                                   the embedder is down
  - memory/context               - tag hits unioned with KNN hits, deduped,
                                   distance-capped, coverage split tag_n/semantic_n
  - kwim_api.backfill_embeddings      - facts committed without a vector get one, in place

The regression that motivated all of it: a subject the caller could not name as an
exact `about` tag returned knowledge: [] even with the facts committed and queryable.
"""
from typing import Any

import pytest

from kwim_api.auth import TeamContext
from kwim_api.routers.memory import memory_context as _memory_context_handler

# ---------------------------------------------------------------------------
# Scriptable fake graph (same shape as test_forget_semantic's)
# ---------------------------------------------------------------------------

class _ScriptedGraph:
    """Returns a queued result_set per query, capturing the Cypher as it goes."""

    def __init__(self, results: list[Any], captured: list[dict]):
        self._results = list(results)
        self._captured = captured

    async def query(self, cypher: str, params: dict | None = None) -> Any:
        self._captured.append({"cypher": cypher, "params": params})
        rs = self._results.pop(0) if self._results else []
        if isinstance(rs, Exception):
            raise rs

        class FakeRes:
            result_set = rs

        return FakeRes()


def _store(results: list[Any]):
    """A FalkorStore whose graph replays `results`, one entry per query call."""
    from kwim_api.stores.falkor import FalkorStore

    captured: list[dict] = []
    graph = _ScriptedGraph(results, captured)
    fs = FalkorStore.__new__(FalkorStore)
    fs._inited = {"kwim_acme"}  # skip schema init
    fs._db = type("FakeDB", (), {"select_graph": lambda self, name: graph})()  # type: ignore[misc]
    return fs, captured


def _row(fid: str, statement: str, score: float, about: list[str] | None = None,
         fact_type: str = "product", decay_class: str = "slow"):
    """One `_fact_projection` row + trailing score, in column order."""
    return [fid, statement, fact_type, "current", "1750000000000", about or [],
            decay_class, "agent_proposal", None, score]


# ---------------------------------------------------------------------------
# FalkorStore.search_facts
# ---------------------------------------------------------------------------

async def test_search_facts_unfiltered_uses_vector_index():
    fs, captured = _store([[_row("f1", "The Widget ships with a spare gasket.", 0.12)]])
    rows = await fs.search_facts("acme", [0.1, 0.2], limit=5)

    assert rows == [{
        "id": "f1", "statement": "The Widget ships with a spare gasket.", "fact_type": "product",
        "status": "current", "created_at": "1750000000000", "about": [],
        "decay_class": "slow", "source_kind": "agent_proposal", "last_verified_at": None,
        "score": 0.12,
    }]
    cypher = captured[0]["cypher"]
    assert "db.idx.vector.queryNodes('Fact', 'embedding'" in cypher
    assert "node.status='current'" in cypher
    assert "ORDER BY score ASC" in cypher
    assert captured[0]["params"]["k"] == 5


async def test_search_facts_filtered_scans_instead_of_indexing():
    """Filtered search has to filter before it scores - going through the index
    would apply the filter after the top-k cut and return nothing off-cliff."""
    fs, captured = _store([[_row("f1", "Widgets ship sealed.", 0.3, about=["Widget"])]])
    rows = await fs.search_facts("acme", [0.1], limit=5, about=["widget"])

    assert [r["id"] for r in rows] == ["f1"]
    cypher = captured[0]["cypher"]
    assert "db.idx.vector.queryNodes" not in cypher
    assert "vec.cosineDistance(f.embedding, vecf32($qvec))" in cypher
    assert "f.embedding IS NOT NULL" in cypher
    # Case-insensitive ANY-membership - the same semantics knowledge/query uses.
    assert "toLower(a) = toLower(qa)" in cypher
    assert captured[0]["params"]["about"] == ["widget"]


async def test_search_facts_fact_type_filter_scopes_the_scan():
    fs, captured = _store([[]])
    await fs.search_facts("acme", [0.1], limit=5, fact_type="policy")
    assert "f.fact_type=$fact_type" in captured[0]["cypher"]
    assert captured[0]["params"]["fact_type"] == "policy"


async def test_search_facts_empty_index_returns_empty_not_raises():
    """A team with no embedded facts must not 500 the endpoint."""
    fs, _ = _store([RuntimeError("no such index")])
    assert await fs.search_facts("acme", [0.1]) == []


async def test_search_facts_row_mapping_defaults():
    fs, _ = _store([[["f1", "s", "product", "current", "1750000000000", None,
                      None, None, None, 0.5]]])
    row = (await fs.search_facts("acme", [0.1]))[0]
    assert row["about"] == []
    assert row["decay_class"] == "slow"
    assert row["source_kind"] is None
    assert row["last_verified_at"] is None


async def test_search_facts_matches_query_facts_row_shape():
    """Both feed _enrich_facts and get unioned in memory/context - the keys must
    be identical or the bundle grows ragged rows."""
    fs, _ = _store([[_row("f1", "s", 0.1)]])
    sem = (await fs.search_facts("acme", [0.1]))[0]
    fs2, _ = _store([[["f1", "s", "product", "current", "1750000000000", [],
                       "slow", "agent_proposal", None]]])
    tag = (await fs2.query_facts("acme", None, "current", 10))[0]
    assert set(sem) - {"score"} == set(tag)


# ---------------------------------------------------------------------------
# GET /v1/knowledge/search
# ---------------------------------------------------------------------------

class _FakeFalkor:
    def __init__(self, facts: list[dict] | None = None):
        self.facts = facts or []
        self.calls: list[dict] = []

    async def search_facts(self, team, qvec, limit=10, about=None, fact_type=None):
        self.calls.append({"team": team, "limit": limit, "about": about,
                           "fact_type": fact_type})
        return self.facts


class _FakeEmbedder:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("embedder down")
        return [[0.1, 0.2, 0.3] for _ in texts]


DEV = {"Authorization": "Bearer devkey"}


@pytest.fixture
def wired(client, monkeypatch):
    from kwim_api.runtime import State

    falkor, embedder = _FakeFalkor(), _FakeEmbedder()
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    monkeypatch.setattr(State, "embedder", embedder, raising=False)
    return client, falkor, embedder


def _fact(fid: str, score: float, created_at: str = "2026-08-01T00:00:00+00:00",
          decay_class: str = "slow"):
    return {"id": fid, "statement": f"statement {fid}", "fact_type": "product",
            "status": "current", "created_at": created_at, "about": [],
            "decay_class": decay_class, "source_kind": "agent_proposal",
            "last_verified_at": None, "score": score}


def test_knowledge_search_returns_scored_facts(wired):
    client, falkor, _ = wired
    falkor.facts = [_fact("f1", 0.11), _fact("f2", 0.42)]

    r = client.get("/v1/knowledge/search?q=what+ships+with+the+widget", headers=DEV)
    assert r.status_code == 200
    body = r.json()
    assert [f["id"] for f in body] == ["f1", "f2"]
    assert body[0]["score"] == 0.11
    assert body[0]["freshness"] in ("fresh", "aging", "stale")
    assert body[0]["as_of"]


def test_knowledge_search_keeps_distance_ranking_over_freshness(wired):
    """/query sorts fresh-first; /search must not - the nearest match leads even
    when an older fact is fresher."""
    client, falkor, _ = wired
    stale_but_nearest = _fact("near", 0.05, created_at="2020-01-01T00:00:00+00:00",
                              decay_class="fast")
    fresh_but_far = _fact("far", 0.55, created_at="2026-08-11T00:00:00+00:00")
    falkor.facts = [stale_but_nearest, fresh_but_far]

    body = client.get("/v1/knowledge/search?q=x", headers=DEV).json()
    assert [f["id"] for f in body] == ["near", "far"]
    assert body[0]["freshness"] == "stale"


def test_knowledge_search_503_when_embedder_down(client, monkeypatch):
    """Never a silent [] - that is indistinguishable from "we know nothing"."""
    from kwim_api.runtime import State

    monkeypatch.setattr(State, "falkor", _FakeFalkor(), raising=False)
    monkeypatch.setattr(State, "embedder", _FakeEmbedder(fail=True), raising=False)

    r = client.get("/v1/knowledge/search?q=anything", headers=DEV)
    assert r.status_code == 503
    assert "embedder" in r.json()["detail"]


def test_knowledge_search_passes_filters_through(wired):
    client, falkor, _ = wired
    client.get("/v1/knowledge/search?q=x&limit=3&fact_type=policy&about=Acme+Corp",
               headers=DEV)
    assert falkor.calls[-1] == {"team": "acme", "limit": 3, "about": ["Acme Corp"],
                                "fact_type": "policy"}


def test_knowledge_search_requires_q(wired):
    client, _, _ = wired
    assert client.get("/v1/knowledge/search", headers=DEV).status_code == 422


# ---------------------------------------------------------------------------
# memory/context - tag + semantic union
# ---------------------------------------------------------------------------

class _CtxFalkor:
    def __init__(self, tag_facts=None, sem_facts=None):
        self._tag = tag_facts or []
        self._sem = sem_facts or []
        self.search_calls: list[dict] = []

    async def query_facts(self, team, fact_type, status, limit, about=None,
                          source_kind=None):
        if about:
            return [f for f in self._tag
                    if any(a.lower() in [x.lower() for x in f.get("about", [])]
                           for a in about)]
        return self._tag

    async def search_facts(self, team, qvec, limit=10, about=None, fact_type=None):
        self.search_calls.append({"limit": limit})
        return self._sem

    async def query_rules(self, team, situation=None, limit=20):
        return []


class _CtxPg:
    async def recent_episodic(self, team, session_id):
        return []


class _CtxState:
    def __init__(self, falkor, embedder):
        self.falkor = falkor
        self.pg = _CtxPg()
        self.embedder = embedder


@pytest.fixture
def call_context(monkeypatch):
    import kwim_api.routers.memory as main_mod

    async def _call(subject, tag_facts=None, sem_facts=None, embedder=None):
        falkor = _CtxFalkor(tag_facts=tag_facts, sem_facts=sem_facts)
        monkeypatch.setattr(main_mod, "State",
                            _CtxState(falkor, embedder or _FakeEmbedder()))
        team = TeamContext(team="acme", key_id="devkey")
        result = await _memory_context_handler(
            session_id="s1", subject=subject, team=team)
        return result, falkor

    return _call


async def test_free_text_subject_retrieves_via_knn(call_context):
    """The regression this whole path exists for: a subject that matches no
    `about` tag used to return
    knowledge: [] with the fact sitting right there."""
    sem = [_fact("f1", 0.18)]
    result, _ = await call_context("what ships with the widget", tag_facts=[], sem_facts=sem)

    assert [f["id"] for f in result["knowledge"]] == ["f1"]
    cov = result["coverage"]["knowledge"]
    assert cov["covered"] is True
    assert cov["queried"] is True
    assert cov["tag_n"] == 0
    assert cov["semantic_n"] == 1


async def test_tag_hits_lead_and_dedupe_against_knn(call_context):
    tag = [{"id": "f1", "statement": "tagged", "fact_type": "product",
            "status": "current", "created_at": "2026-08-01T00:00:00+00:00",
            "about": ["Widget"], "decay_class": "slow", "source_kind": None,
            "last_verified_at": None}]
    sem = [_fact("f1", 0.05), _fact("f2", 0.2)]   # f1 also came back from the KNN
    result, _ = await call_context("Widget", tag_facts=tag, sem_facts=sem)

    ids = [f["id"] for f in result["knowledge"]]
    assert ids == ["f1", "f2"]        # tag hit first, no duplicate f1
    cov = result["coverage"]["knowledge"]
    assert cov["tag_n"] == 1
    assert cov["semantic_n"] == 1     # only f2 was actually added
    assert cov["n"] == 2


async def test_distance_cutoff_drops_far_matches(call_context):
    """Unbounded KNN always returns its k nearest, however unrelated. Anything past
    retrieval.context_semantic_max_dist (0.6) must not reach the prompt. The
    fixtures straddle it: measured live, a named-entity question puts the right
    fact at 0.25-0.55 and the next-best at 0.63+."""
    sem = [_fact("near", 0.2), _fact("far", 0.95)]
    result, _ = await call_context("subject", tag_facts=[], sem_facts=sem)

    assert [f["id"] for f in result["knowledge"]] == ["near"]
    assert result["coverage"]["knowledge"]["semantic_n"] == 1


async def test_context_rows_have_uniform_shape(call_context):
    """The bundle is one flat list - a `score` on half the rows is a ragged shape
    for whatever consumes it."""
    tag = [{"id": "t1", "statement": "tagged", "fact_type": "product",
            "status": "current", "created_at": "2026-08-01T00:00:00+00:00",
            "about": ["Widget"], "decay_class": "slow", "source_kind": None,
            "last_verified_at": None}]
    result, _ = await call_context("Widget", tag_facts=tag, sem_facts=[_fact("s1", 0.2)])

    shapes = {frozenset(f) for f in result["knowledge"]}
    assert len(shapes) == 1
    assert "score" not in next(iter(shapes))


async def test_embedder_down_degrades_to_tag_only(call_context):
    """The context bundle stays fail-soft: no semantic half, but the tag half and
    the rest of the bundle still come back."""
    tag = [{"id": "t1", "statement": "tagged", "fact_type": "product",
            "status": "current", "created_at": "2026-08-01T00:00:00+00:00",
            "about": ["Widget"], "decay_class": "slow", "source_kind": None,
            "last_verified_at": None}]
    result, falkor = await call_context("Widget", tag_facts=tag, sem_facts=[_fact("s1", 0.1)],
                                        embedder=_FakeEmbedder(fail=True))

    assert [f["id"] for f in result["knowledge"]] == ["t1"]
    assert falkor.search_calls == []          # never attempted without a vector
    assert result["coverage"]["knowledge"]["semantic_n"] == 0


async def test_no_subject_skips_both_halves(call_context):
    result, falkor = await call_context(None, tag_facts=[], sem_facts=[_fact("s1", 0.1)])
    assert result["knowledge"] == []
    assert result["coverage"]["knowledge"]["queried"] is False
    assert falkor.search_calls == []


# ---------------------------------------------------------------------------
# FalkorStore backfill helpers
# ---------------------------------------------------------------------------

async def test_facts_missing_embedding_selects_current_unembedded():
    fs, captured = _store([[["f1", "a statement"], ["f2", None]]])
    rows = await fs.facts_missing_embedding("acme", limit=50)

    assert rows == [{"id": "f1", "statement": "a statement"},
                    {"id": "f2", "statement": ""}]
    cypher = captured[0]["cypher"]
    assert "f.status='current'" in cypher
    assert "f.embedding IS NULL" in cypher
    assert captured[0]["params"]["limit"] == 50


async def test_set_fact_embedding_writes_and_verifies():
    fs, captured = _store([[], [["f1"]]])
    assert await fs.set_fact_embedding("acme", "f1", [0.1, 0.2]) is True
    assert "SET f.embedding=vecf32($embedding)" in captured[0]["cypher"]
    # Reads back rather than trusting the write.
    assert "f.embedding IS NOT NULL" in captured[1]["cypher"]


async def test_set_fact_embedding_reports_false_when_not_applied():
    fs, _ = _store([[], []])
    assert await fs.set_fact_embedding("acme", "gone", [0.1]) is False


async def test_set_fact_embedding_touches_only_the_vector():
    fs, captured = _store([[], [["f1"]]])
    await fs.set_fact_embedding("acme", "f1", [0.1])
    write = captured[0]["cypher"]
    for prop in ("f.statement", "f.status", "f.about", "f.decay_class"):
        assert prop not in write


# ---------------------------------------------------------------------------
# kwim_api.backfill_embeddings - plan / execute
# ---------------------------------------------------------------------------

class _BackfillStore:
    def __init__(self, missing: list[dict], fail_write: set[str] | None = None):
        self._missing = missing
        self._fail_write = fail_write or set()
        self.written: dict[str, list[float]] = {}

    async def facts_missing_embedding(self, team: str, limit: int = 1000) -> list[dict]:
        return self._missing[:limit]

    async def set_fact_embedding(self, team: str, fact_id: str, embedding) -> bool:
        if fact_id in self._fail_write:
            return False
        self.written[fact_id] = embedding
        return True


async def test_plan_backfill_separates_blank_statements():
    from kwim_api.backfill_embeddings import plan_backfill

    store = _BackfillStore([{"id": "f1", "statement": "real"},
                            {"id": "f2", "statement": "   "}])
    embeddable, skipped = await plan_backfill(store, "acme", 100)
    assert [r["id"] for r in embeddable] == ["f1"]
    assert [r["id"] for r in skipped] == ["f2"]


async def test_execute_backfill_embeds_and_counts():
    from kwim_api.backfill_embeddings import execute_backfill

    store = _BackfillStore([])
    plan = [{"id": "f1", "statement": "one"}, {"id": "f2", "statement": "two"}]
    report = await execute_backfill(store, _FakeEmbedder(), "acme", plan)

    assert report == {"embedded": 2, "failed": []}
    assert set(store.written) == {"f1", "f2"}


async def test_execute_backfill_reports_unverified_writes():
    from kwim_api.backfill_embeddings import execute_backfill

    store = _BackfillStore([], fail_write={"f2"})
    plan = [{"id": "f1", "statement": "one"}, {"id": "f2", "statement": "two"}]
    report = await execute_backfill(store, _FakeEmbedder(), "acme", plan)

    assert report["embedded"] == 1
    assert report["failed"] == ["f2"]


async def test_execute_backfill_survives_embedder_failure():
    """A dead embedder must not abort the run silently as success - the facts stay
    un-embedded and get picked up next time."""
    from kwim_api.backfill_embeddings import execute_backfill

    store = _BackfillStore([])
    plan = [{"id": "f1", "statement": "one"}]
    report = await execute_backfill(store, _FakeEmbedder(fail=True), "acme", plan)

    assert report == {"embedded": 0, "failed": ["f1"]}
    assert store.written == {}


async def test_backfill_team_dry_run_writes_nothing():
    from kwim_api.backfill_embeddings import backfill_team

    store = _BackfillStore([{"id": "f1", "statement": "real"}])
    outstanding = await backfill_team(store, None, "acme", 100, commit=False)

    assert outstanding == 1
    assert store.written == {}
