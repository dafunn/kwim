"""Tests for document search (fusion, expansion) and the /v1/documents routes."""
import dataclasses

import pytest

from kwim_api.config import settings
from kwim_api.documents import search as search_mod
from kwim_api.documents.search import fulltext_query, rrf, search
from kwim_api.runtime import State

DOC = "d1"
UDOC = "u1"


class FakeDocsFalkor:
    """Two graphs: acme (DOC) and universe (UDOC). Search results are scripted."""

    def __init__(self):
        self.vector: dict[str, list[dict]] = {"acme": [], "universe": []}
        self.fulltext: dict[str, list[dict]] = {"acme": [], "universe": []}
        self.calls: list[tuple] = []
        self.sections = {
            ("acme", f"{DOC}:s1"): _section(DOC, "s1", "Removal", ["c1", "c2"]),
            ("acme", f"{DOC}:s2"): _section(DOC, "s2", "Inspection", ["c3"]),
            ("universe", f"{UDOC}:s1"): _section(UDOC, "s1", "Shared", ["c1"]),
        }
        self.docs = {"acme": [{"id": DOC, "title": "Pump", "tags": ["pumps"]}],
                     "universe": [{"id": UDOC, "title": "Shared", "tags": []}]}

    async def docs_vector_search(self, team, qvec, k, document_ids=None):
        self.calls.append(("vector", team, k, document_ids))
        return list(self.vector[team])

    async def docs_fulltext_search(self, team, query, k, document_ids=None):
        self.calls.append(("fulltext", team, query, document_ids))
        return list(self.fulltext[team])

    async def docs_documents(self, team, tag=None):
        return [dict(d) for d in self.docs[team] if tag is None or tag in d["tags"]]

    async def docs_section(self, team, section_id):
        s = self.sections.get((team, section_id))
        return dict(s) if s else None

    async def docs_has_document(self, team, document_id):
        return any(d["id"] == document_id for d in self.docs[team])

    async def docs_figure(self, team, document_id, figure_id):
        if (team, document_id, figure_id) == ("acme", DOC, "f1"):
            return {"caption": "Figure 1", "page": 2, "object_key": "figures/" + "a" * 64 + "/f1.png"}
        return None


def _section(doc, sid, heading, chunk_ids):
    return {"id": f"{doc}:{sid}", "document_id": doc, "heading": heading,
            "path": f"Pump > {heading}", "level": 2, "page_start": 3, "page_end": 4,
            "document_title": "Pump", "revision_number": 2,
            "chunks": [{"id": f"{doc}:{c}", "seq": i, "text": f"text of {c}",
                        "page_start": 3 + i, "page_end": 3 + i}
                       for i, c in enumerate(chunk_ids)],
            "figures": [{"figure_id": "f1", "caption": "Figure 1", "page": 3,
                         "object_key": "figures/x/f1.png"}]}


def _hit(doc, cid, sid, score):
    return {"id": f"{doc}:{cid}", "document_id": doc, "section_id": f"{doc}:{sid}",
            "seq": 0, "text": f"text of {cid}", "heading_path": "", "page_start": 3,
            "page_end": 3, "score": score}


class FakeEmbedder:
    def __init__(self, fail=False):
        self.fail = fail

    async def embed(self, texts):
        if self.fail:
            raise RuntimeError("down")
        return [[0.1, 0.2] for _ in texts]


def test_fulltext_query_keeps_terms_and_drops_syntax():
    assert fulltext_query('borg: "weird" (query) @x 6379') == "borg|weird|query|x|6379"
    assert fulltext_query("Restore restore RESTORE") == "Restore"
    assert fulltext_query("?? !!") is None


def test_rrf_rewards_chunks_found_by_both_methods():
    a = [{"team": "t", "id": "x"}, {"team": "t", "id": "y"}]
    b = [{"team": "t", "id": "y"}, {"team": "t", "id": "z"}]
    fused = rrf(a, b, k=60)
    assert [c["id"] for c in fused] == ["y", "x", "z"]
    assert fused[0]["rrf"] == pytest.approx(1 / 62 + 1 / 61)


async def test_search_groups_hits_by_section_and_cites():
    f = FakeDocsFalkor()
    f.vector["acme"] = [_hit(DOC, "c2", "s1", 0.1), _hit(DOC, "c3", "s2", 0.2)]
    f.fulltext["acme"] = [_hit(DOC, "c1", "s1", 5.0)]
    out = await search(f, FakeEmbedder(), "acme", "remove the pump", limit=5)
    assert out["coverage"] == {"vector": True, "fulltext": True, "reason": None,
                               "reranked": False}
    first = out["results"][0]
    assert first["section_id"] == "s1" and first["heading"] == "Removal"
    assert set(first["matched_chunks"]) == {"c1", "c2"}
    assert first["text"] == "text of c1\n\ntext of c2"
    assert (first["page_start"], first["page_end"]) == (3, 4)
    assert first["document_title"] == "Pump" and first["revision_number"] == 2
    assert first["scope"] == "team" and first["complete_section"] is True
    assert first["figures"][0]["url"] == f"/v1/documents/{DOC}/figures/f1"
    assert [r["section_id"] for r in out["results"]] == ["s1", "s2"]


async def test_search_covers_the_universe_graph():
    f = FakeDocsFalkor()
    f.fulltext["universe"] = [_hit(UDOC, "c1", "s1", 3.0)]
    out = await search(f, FakeEmbedder(), "acme", "shared", limit=5)
    assert [(r["scope"], r["document_id"]) for r in out["results"]] == [("universe", UDOC)]
    assert {c[1] for c in f.calls} == {"acme", "universe"}


async def test_search_limit_counts_sections():
    f = FakeDocsFalkor()
    f.vector["acme"] = [_hit(DOC, "c1", "s1", 0.1), _hit(DOC, "c3", "s2", 0.2)]
    out = await search(f, FakeEmbedder(), "acme", "q", limit=1)
    assert [r["section_id"] for r in out["results"]] == ["s1"]


async def test_search_falls_back_to_fulltext_without_an_embedder():
    f = FakeDocsFalkor()
    f.fulltext["acme"] = [_hit(DOC, "c3", "s2", 1.0)]
    for embedder, reason in ((None, "no document embedder configured"),
                             (FakeEmbedder(fail=True), "embedder unavailable")):
        out = await search(f, embedder, "acme", "impeller", limit=5)
        assert out["coverage"]["vector"] is False and reason in out["coverage"]["reason"]
        assert [r["section_id"] for r in out["results"]] == ["s2"]


async def test_search_by_tag_scopes_each_graph():
    f = FakeDocsFalkor()
    out = await search(f, FakeEmbedder(), "acme", "q", limit=5, tag="pumps")
    assert out["results"] == []
    assert ("vector", "acme", settings.documents_search_vector_k, [DOC]) in f.calls
    assert not any(c[1] == "universe" for c in f.calls)
    out = await search(f, FakeEmbedder(), "acme", "q", limit=5, tag="nothing")
    assert out["coverage"]["reason"] == "no matching documents"


async def test_a_large_section_returns_the_hit_neighbourhood(monkeypatch):
    monkeypatch.setattr(search_mod, "settings", dataclasses.replace(
        settings, documents_search_max_section_tokens=1))
    f = FakeDocsFalkor()
    f.sections[("acme", f"{DOC}:s1")] = _section(DOC, "s1", "Removal",
                                                 ["c1", "c2", "c3", "c4", "c5"])
    f.vector["acme"] = [_hit(DOC, "c3", "s1", 0.1)]
    out = await search(f, FakeEmbedder(), "acme", "q", limit=5)
    r = out["results"][0]
    assert r["complete_section"] is False
    assert r["text"] == "text of c2\n\ntext of c3\n\ntext of c4"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def docs_state(monkeypatch):
    f = FakeDocsFalkor()
    monkeypatch.setattr(State, "falkor", f, raising=False)
    monkeypatch.setattr(State, "docs_embedder", FakeEmbedder(), raising=False)
    return f


H = {"Authorization": "Bearer devkey"}


def test_routes_require_a_team_key(client, docs_state):
    for path in ("/v1/documents", "/v1/documents/search?q=x",
                 f"/v1/documents/{DOC}/sections/s1", f"/v1/documents/{DOC}/figures/f1"):
        assert client.get(path).status_code == 401


def test_list_includes_team_and_universe_documents(client, docs_state):
    r = client.get("/v1/documents", headers=H)
    assert r.status_code == 200
    assert [(d["id"], d["scope"]) for d in r.json()["items"]] == [(DOC, "team"),
                                                                   (UDOC, "universe")]


def test_search_route(client, docs_state):
    docs_state.fulltext["acme"] = [_hit(DOC, "c3", "s2", 1.0)]
    r = client.get("/v1/documents/search", params={"q": "impeller"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["heading"] == "Inspection"
    assert client.get("/v1/documents/search", headers=H).status_code == 422


def test_section_route_finds_team_and_universe_documents(client, docs_state):
    r = client.get(f"/v1/documents/{DOC}/sections/s1", headers=H)
    assert r.status_code == 200 and r.json()["text"] == "text of c1\n\ntext of c2"
    r = client.get(f"/v1/documents/{UDOC}/sections/s1", headers=H)
    assert r.status_code == 200 and r.json()["heading"] == "Shared"
    assert client.get(f"/v1/documents/{DOC}/sections/s9", headers=H).status_code == 404
    assert client.get("/v1/documents/nope/sections/s1", headers=H).status_code == 404


def test_figure_route_serves_the_stored_png(client, docs_state, tmp_path):
    object.__setattr__(settings, "documents_storage_path", str(tmp_path))
    try:
        path = tmp_path / "acme" / "figures" / ("a" * 64)
        path.mkdir(parents=True)
        (path / "f1.png").write_bytes(b"\x89PNG fake")
        r = client.get(f"/v1/documents/{DOC}/figures/f1", headers=H)
        assert r.status_code == 200 and r.content == b"\x89PNG fake"
        assert r.headers["content-type"] == "image/png"
        assert client.get(f"/v1/documents/{DOC}/figures/f2", headers=H).status_code == 404
    finally:
        object.__setattr__(settings, "documents_storage_path", "/data/documents")


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

class FakeReranker:
    def __init__(self, prefer=None, fail=False):
        self.prefer = prefer
        self.fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    async def scores(self, query, texts):
        self.calls.append((query, texts))
        if self.fail:
            raise RuntimeError("reranker down")
        return [10.0 if self.prefer and self.prefer in t else 0.0 for t in texts]


async def test_reranker_reorders_sections():
    f = FakeDocsFalkor()
    f.vector["acme"] = [_hit(DOC, "c1", "s1", 0.1), _hit(DOC, "c3", "s2", 0.2)]
    rr = FakeReranker(prefer="text of c3")
    out = await search(f, FakeEmbedder(), "acme", "impeller", limit=5, reranker=rr)
    assert out["coverage"]["reranked"] is True
    assert [r["section_id"] for r in out["results"]] == ["s2", "s1"]
    assert rr.calls[0][0] == "impeller"
    assert rr.calls[0][1][0] == ":\ntext of c1"


async def test_reranker_failure_keeps_the_fused_order():
    f = FakeDocsFalkor()
    f.vector["acme"] = [_hit(DOC, "c1", "s1", 0.1), _hit(DOC, "c3", "s2", 0.2)]
    out = await search(f, FakeEmbedder(), "acme", "q", limit=5,
                       reranker=FakeReranker(fail=True))
    assert out["coverage"]["reranked"] is False
    assert [r["section_id"] for r in out["results"]] == ["s1", "s2"]


async def test_only_the_top_candidates_are_reranked(monkeypatch):
    monkeypatch.setattr(search_mod, "settings", dataclasses.replace(
        settings, documents_reranker_candidates=1))
    f = FakeDocsFalkor()
    f.vector["acme"] = [_hit(DOC, "c1", "s1", 0.1), _hit(DOC, "c3", "s2", 0.2)]
    rr = FakeReranker(prefer="text of c3")
    await search(f, FakeEmbedder(), "acme", "q", limit=5, reranker=rr)
    assert len(rr.calls[0][1]) == 1


async def test_reranker_client_splits_batches_and_keeps_order():
    import json as _json

    import httpx

    from kwim_api.documents.rerank import Reranker

    sizes: list[int] = []

    def handler(request):
        body = _json.loads(request.content)
        sizes.append(len(body["texts"]))
        assert body["truncate"] is True and body["raw_scores"] is True
        items = [{"index": i, "score": float(t)} for i, t in enumerate(body["texts"])]
        return httpx.Response(200, json=list(reversed(items)))

    rr = Reranker("http://rr.test", max_batch=32)
    rr._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scores = await rr.scores("q", [str(i) for i in range(40)])
    assert sizes == [32, 8]
    assert scores == [float(i) for i in range(40)]
    await rr.close()


async def test_a_promoted_team_document_is_found_only_through_its_universe_copy():
    f = FakeDocsFalkor()
    f.docs["acme"][0]["promoted_to"] = UDOC
    f.fulltext["acme"] = [_hit(DOC, "c1", "s1", 9.0)]
    f.fulltext["universe"] = [_hit(UDOC, "c1", "s1", 1.0)]
    out = await search(f, FakeEmbedder(), "acme", "pump", limit=5)
    assert [(r["scope"], r["document_id"]) for r in out["results"]] == [("universe", UDOC)]
