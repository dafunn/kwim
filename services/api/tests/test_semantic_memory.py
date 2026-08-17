"""Pure-logic tests for the semantic memory slice.

Covers:
  - SemanticItem model validation.
  - Embedder client request formatting.
  - SemanticConsumer.handle logic (skip rules, embed failure handling, metadata).
  - FalkorStore semantic query Cypher construction + metadata promotion.
"""
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# SemanticItem model
# ---------------------------------------------------------------------------

def test_semantic_item_model():
    from kwim_api.models import SemanticItem

    item = SemanticItem(id="ev-1", content="hello world", score=0.95)
    assert item.metadata == {}

    item_meta = SemanticItem(id="ev-2", content="test", score=0.5,
                             metadata={"event_type": "turn", "agent_id": "a1"})
    assert item_meta.metadata == {"event_type": "turn", "agent_id": "a1"}


# ---------------------------------------------------------------------------
# Embedder client - request formatting
# ---------------------------------------------------------------------------

async def test_embedder_request_formatting():
    from kwim_api.embedder import Embedder

    called: list[dict] = []

    class _FakeResponse:
        def __init__(self, payload: Any):
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return self._payload

    class _FakeClient:
        def __init__(self, response: Any):
            self._response = response

        async def post(self, url: str, *, json: Any = None) -> Any:
            called.append({"url": url, "json": json})
            return self._response

        async def aclose(self) -> None:
            pass

    embedder = Embedder()
    embedder._url = "http://embedder:80"
    embedder._http = _FakeClient(_FakeResponse([[0.1, 0.2, 0.3]]))

    result = await embedder.embed(["hello"])
    assert result == [[0.1, 0.2, 0.3]]
    assert called[-1]["url"] == "http://embedder:80/embed"
    assert called[-1]["json"] == {"inputs": ["hello"]}


# ---------------------------------------------------------------------------
# SemanticConsumer.handle - logic branches
# ---------------------------------------------------------------------------

class _FakeConsumerFalkor:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def materialize_semantic(self, team: str, item: dict) -> None:
        self.calls.append((team, item))


class _FakeEmbedder:
    def __init__(self, response: list[list[float]] | Exception):
        self._response = response

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


async def test_consumer_no_text_skips():
    from kwim_api.semantic_consumer import SemanticConsumer

    ff = _FakeConsumerFalkor()
    sc = SemanticConsumer(ff, _FakeEmbedder([[0.1, 0.2]]), None)  # type: ignore[arg-type]
    await sc.handle("acme", {"event_id": "ev-1", "event_data": {}})
    assert len(ff.calls) == 0


async def test_consumer_text_present_embeds_and_writes():
    from kwim_api.semantic_consumer import SemanticConsumer

    ff = _FakeConsumerFalkor()
    sc = SemanticConsumer(ff, _FakeEmbedder([[0.1, 0.2, 0.3]]), None)  # type: ignore[arg-type]
    await sc.handle("acme", {
        "event_id": "ev-2",
        "agent_id": "agent-x",
        "session_id": "sess-1",
        "event_type": "turn",
        "event_data": {"text": "hello world"},
    })
    assert len(ff.calls) == 1
    team, item = ff.calls[0]
    assert team == "acme"
    assert item["id"] == "ev-2"
    assert item["content"] == "hello world"
    assert item["embedding"] == [0.1, 0.2, 0.3]
    assert item["metadata"].get("event_type") == "turn"
    assert item["metadata"].get("agent_id") == "agent-x"
    assert item["metadata"].get("session_id") == "sess-1"
    assert "event_id" in item["metadata"]
    assert None not in item["metadata"].values()


async def test_consumer_embedder_failure_skips_without_crash():
    from kwim_api.semantic_consumer import SemanticConsumer

    ff = _FakeConsumerFalkor()
    sc = SemanticConsumer(ff, _FakeEmbedder(RuntimeError("embedder down")), None)  # type: ignore[arg-type]
    await sc.handle("acme", {"event_id": "ev-3", "event_data": {"text": "should fail"}})
    assert len(ff.calls) == 0


async def test_consumer_whitespace_only_text_skips():
    from kwim_api.semantic_consumer import SemanticConsumer

    ff = _FakeConsumerFalkor()
    sc = SemanticConsumer(ff, _FakeEmbedder([[0.1]]), None)  # type: ignore[arg-type]
    await sc.handle("acme", {"event_id": "ev-4", "event_data": {"text": "   "}})
    assert len(ff.calls) == 0


# ---------------------------------------------------------------------------
# FalkorStore - metadata promotion + query Cypher construction
# ---------------------------------------------------------------------------

def test_falkor_reserved_keys_block_promotion():
    from kwim_api.stores.falkor import FalkorStore

    assert "embedding" in FalkorStore._SEMANTIC_RESERVED
    assert "id" in FalkorStore._SEMANTIC_RESERVED


@pytest.fixture
def capture_falkor():
    """A FalkorStore whose graph.query() captures the Cypher instead of running it."""
    from kwim_api.stores.falkor import FalkorStore

    captured: list[dict] = []

    class _CaptureGraph:
        async def query(self, cypher: str, params: dict | None = None) -> Any:
            captured.append({"cypher": cypher, "params": params})

            class FakeRes:
                result_set: list[Any] = []

            return FakeRes()

    fs = FalkorStore.__new__(FalkorStore)
    fs._inited = {"kwim_acme"}  # skip init calls
    fs._db = type("FakeDB", (), {"select_graph": lambda self, name: _CaptureGraph()})()  # type: ignore[misc]
    return fs, captured


async def test_query_semantic_vector_with_filters(capture_falkor):
    fs, captured = capture_falkor
    await fs.query_semantic("acme", qvec=[0.1, 0.2], limit=5, filters={"project": "demoproject"})
    assert len(captured) == 1
    cypher = captured[-1]["cypher"]
    assert "db.idx.vector.queryNodes" in cypher
    assert "WHERE" in cypher
    assert "node.project=$filter_project" in cypher
    assert captured[-1]["params"]["k"] == 5
    assert captured[-1]["params"]["qvec"] == [0.1, 0.2]
    assert captured[-1]["params"]["filter_project"] == "demoproject"


async def test_query_semantic_metadata_only(capture_falkor):
    fs, captured = capture_falkor
    await fs.query_semantic("acme", qvec=None, limit=10, filters={"doc_type": "style", "project": "altproject"})
    cypher = captured[-1]["cypher"]
    assert "db.idx.vector.queryNodes" not in cypher
    assert "MATCH (s:SemanticItem)" in cypher
    assert "s.doc_type=$filter_doc_type" in cypher
    assert "s.project=$filter_project" in cypher


async def test_query_semantic_no_filters_has_no_where(capture_falkor):
    fs, captured = capture_falkor
    await fs.query_semantic("acme", qvec=[0.1], limit=3, filters=None)
    assert "WHERE" not in captured[-1]["cypher"]


async def test_get_by_metadata(capture_falkor):
    fs, captured = capture_falkor
    await fs.get_by_metadata("acme", {"project": "demoproject"})
    assert len(captured) == 1
    cypher = captured[-1]["cypher"]
    assert "MATCH (s:SemanticItem)" in cypher
    assert "WHERE" in cypher
    assert "s.project=$filter_project" in cypher
    assert captured[-1]["params"]["filter_project"] == "demoproject"


async def test_get_by_metadata_empty_filters_short_circuits(capture_falkor):
    fs, captured = capture_falkor
    result = await fs.get_by_metadata("acme", {})
    assert result == []
    assert len(captured) == 0
