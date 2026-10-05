"""Pure-logic tests for the FalkorDB rebuild CLI.

Covers:
  - Commit-log replay dispatch (fact commit, rule commit, reinforce, deprecate,
    retract, confirm, unknown-op, parsed-JSONB payloads).
  - created_at restoration from the commit row's committed_at.
  - Semantic re-embed batching.
  - CLI arg validation.
"""
import argparse
import json
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Fake stores for replay tests
# ---------------------------------------------------------------------------

class _FakeFalkor:
    def __init__(self):
        self.calls: list[dict] = []

    async def materialize_fact(self, team, fact, provenance, graph_name=None, embedding=None,
                               created_at=None) -> None:
        self.calls.append({"method": "materialize_fact", "team": team, "fact": fact,
                           "provenance": provenance, "graph_name": graph_name,
                           "embedding": embedding, "created_at": created_at})

    async def materialize_rule(self, team, rule, provenance, graph_name=None,
                               created_at=None) -> None:
        self.calls.append({"method": "materialize_rule", "team": team, "rule": rule,
                           "provenance": provenance, "graph_name": graph_name,
                           "created_at": created_at})

    async def reinforce_rule(self, team, rule_id, evidence, seq, graph_name=None) -> bool:
        self.calls.append({"method": "reinforce_rule", "team": team, "rule_id": rule_id,
                           "evidence": evidence, "seq": seq, "graph_name": graph_name})
        return True

    async def deprecate_rule(self, team, rule_id, graph_name=None) -> None:
        self.calls.append({"method": "deprecate_rule", "team": team, "rule_id": rule_id,
                           "graph_name": graph_name})

    async def materialize_semantic(self, team, item, graph_name=None, created_at=None) -> None:
        self.calls.append({"method": "materialize_semantic", "team": team, "item": item,
                           "graph_name": graph_name, "created_at": created_at})

    async def retract_object(self, team, object_type, object_id, graph_name=None) -> None:
        self.calls.append({"method": "retract_object", "team": team, "object_type": object_type,
                           "object_id": object_id, "graph_name": graph_name})

    async def confirm_object(self, team, object_type, object_id, by, at, graph_name=None) -> None:
        self.calls.append({"method": "confirm_object", "team": team, "object_type": object_type,
                           "object_id": object_id, "by": by, "at": at, "graph_name": graph_name})


class _FakePostgres:
    def __init__(self, commit_rows: list[dict] | None = None, episodic_rows: list[dict] | None = None):
        self.commit_rows = commit_rows or []
        self.episodic_rows = episodic_rows or []

    async def replay_commit_log(self, team: str) -> list[dict]:
        return self.commit_rows

    async def episodic_with_text(self, team: str) -> list[dict]:
        return self.episodic_rows


class _FakeEmbedder:
    def __init__(self, response: list[list[float]]):
        self._response = response
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self._response[: len(texts)]


_OLD = datetime(2020, 9, 13, 12, 26, 40, tzinfo=UTC)
_OLD_MS = int(_OLD.timestamp() * 1000)


def _commit_row(**overrides) -> dict:
    row = {"seq": 1, "object_type": "fact", "object_id": "f1", "operation": "commit",
           "payload": json.dumps({}), "provenance": json.dumps({})}
    row.update(overrides)
    return row


async def _replay(commit_rows, graph_name=None, embedder=None):
    from kwim_api.rebuild import _replay_commit

    ff = _FakeFalkor()
    pg = _FakePostgres(commit_rows=commit_rows)
    await _replay_commit(pg, ff, "acme", graph_name, embedder)
    return ff


# ---------------------------------------------------------------------------
# Replay dispatch
# ---------------------------------------------------------------------------

async def test_replay_fact_commit():
    ff = await _replay([_commit_row(
        seq=1, object_type="fact", object_id="f1", operation="commit",
        payload=json.dumps({"statement": "s1", "fact_type": "ft1", "source_kind": "repo_sync"}),
        provenance=json.dumps({"proposed_by": "agent-a", "supported_by": ["ev1"]}),
    )], graph_name="kwim_acme_rebuild")
    assert len(ff.calls) == 1
    assert ff.calls[0]["method"] == "materialize_fact"
    assert ff.calls[0]["graph_name"] == "kwim_acme_rebuild"
    assert ff.calls[0]["fact"]["id"] == "f1"
    assert ff.calls[0]["fact"]["commit_seq"] == 1
    assert ff.calls[0]["provenance"]["proposed_by"] == "agent-a"


async def test_replay_rule_commit():
    ff = await _replay([_commit_row(
        seq=2, object_type="rule", object_id="r1", operation="commit",
        payload=json.dumps({"rule_type": "advisory", "situation": {"project": "x"}, "approach": "do y"}),
        provenance=json.dumps({"proposed_by": "agent-b", "learned_from": ["ev2"]}),
    )])
    assert ff.calls[0]["method"] == "materialize_rule"
    assert ff.calls[0]["graph_name"] is None
    assert ff.calls[0]["rule"]["status"] == "approved"
    assert "scope" not in ff.calls[0]["rule"]  # materialize_rule defaults it


async def test_replay_rule_reinforce():
    ff = await _replay([_commit_row(
        seq=3, object_type="rule", object_id="r1", operation="reinforce",
        payload=json.dumps({"evidence": ["ev3", "ev4"]}),
    )])
    assert ff.calls[0]["method"] == "reinforce_rule"
    assert ff.calls[0]["evidence"] == ["ev3", "ev4"]
    assert ff.calls[0]["seq"] == 3


async def test_replay_rule_deprecate():
    ff = await _replay([_commit_row(seq=4, object_type="rule", object_id="r1", operation="deprecate")])
    assert ff.calls[0]["method"] == "deprecate_rule"
    assert ff.calls[0]["rule_id"] == "r1"


async def test_replay_unknown_op_no_call():
    ff = await _replay([_commit_row(seq=5, object_type="fact", object_id="f2", operation="unknown_op")])
    assert len(ff.calls) == 0


async def test_replay_retract():
    ff = await _replay([_commit_row(
        seq=7, object_type="fact", object_id="f1", operation="retract",
        provenance=json.dumps({"retracted_by": "alice", "retracted_via": "api",
                               "retracted_at": "2026-06-12T00:00:00Z"}),
    )])
    assert ff.calls[0]["method"] == "retract_object"
    assert ff.calls[0]["object_type"] == "fact"
    assert ff.calls[0]["object_id"] == "f1"


async def test_replay_confirm():
    ff = await _replay([_commit_row(
        seq=8, object_type="rule", object_id="r1", operation="confirm",
        provenance=json.dumps({"confirmed_by": "alice", "confirmed_via": "mattermost",
                               "confirmed_at": "2026-06-12T00:00:00Z"}),
    )])
    assert ff.calls[0]["method"] == "confirm_object"
    assert ff.calls[0]["object_type"] == "rule"
    assert ff.calls[0]["object_id"] == "r1"
    assert ff.calls[0]["by"] == "alice"
    assert ff.calls[0]["at"] == "2026-06-12T00:00:00Z"


async def test_replay_parsed_jsonb_payload():
    # psycopg may return payload/provenance already parsed as dicts (not JSON text).
    ff = await _replay([_commit_row(
        seq=6, object_type="fact", object_id="f3", operation="commit",
        payload={"statement": "s3", "fact_type": "ft3"},
        provenance={"proposed_by": "agent-c"},
    )])
    assert ff.calls[0]["fact"]["statement"] == "s3"


# ---------------------------------------------------------------------------
# Semantic replay (directly written items only)
# ---------------------------------------------------------------------------

def _semantic_row(**overrides) -> dict:
    row = _commit_row(
        seq=1, object_type="semantic", object_id="s1", operation="commit",
        payload=json.dumps({"content": "keep me", "metadata": {"k": "v"}}),
    )
    row.update(overrides)
    return row


async def test_replay_semantic_commit_materializes():
    ff = await _replay([_semantic_row()], embedder=_FakeEmbedder([[0.5] * 3]))
    assert len(ff.calls) == 1
    assert ff.calls[0]["method"] == "materialize_semantic"
    assert ff.calls[0]["item"]["id"] == "s1"
    assert ff.calls[0]["item"]["content"] == "keep me"
    assert ff.calls[0]["item"]["metadata"] == {"k": "v"}
    assert ff.calls[0]["item"]["embedding"] == [0.5, 0.5, 0.5]


async def test_replay_semantic_restores_committed_at():
    ff = await _replay([_semantic_row(committed_at=_OLD)],
                       embedder=_FakeEmbedder([[0.5] * 3]))
    assert ff.calls[0]["created_at"] == _OLD_MS


async def test_replay_semantic_targets_the_temp_graph():
    ff = await _replay([_semantic_row()], graph_name="kwim_acme_rebuild",
                       embedder=_FakeEmbedder([[0.5] * 3]))
    assert ff.calls[0]["graph_name"] == "kwim_acme_rebuild"


async def test_replay_semantic_without_embedder_is_skipped():
    """--skip-semantic leaves the item in commit_log; a later rebuild restores it."""
    ff = await _replay([_semantic_row()], embedder=None)
    assert ff.calls == []


async def test_replay_semantic_embed_failure_does_not_abort():
    class _Boom:
        async def embed(self, texts):
            raise RuntimeError("embedder down")

    ff = await _replay([_semantic_row(), _commit_row(
        seq=2, object_type="fact", object_id="f1", operation="commit",
        payload=json.dumps({"statement": "s", "fact_type": "ft"}))], embedder=_Boom())
    # The semantic row is skipped, but the fact after it still replays.
    assert [c["method"] for c in ff.calls] == ["materialize_fact"]


# ---------------------------------------------------------------------------
# created_at restoration from committed_at
# ---------------------------------------------------------------------------

def test_epoch_ms_accepts_datetime():
    from kwim_api.rebuild import _epoch_ms

    assert _epoch_ms(_OLD) == _OLD_MS


def test_epoch_ms_accepts_iso_string():
    from kwim_api.rebuild import _epoch_ms

    assert _epoch_ms("2020-09-13T12:26:40+00:00") == _OLD_MS


def test_epoch_ms_none_and_unparseable():
    from kwim_api.rebuild import _epoch_ms

    assert _epoch_ms(None) is None
    assert _epoch_ms("not a timestamp") is None


async def test_replay_fact_restores_committed_at():
    ff = await _replay([_commit_row(
        seq=1, object_type="fact", object_id="f1", operation="commit",
        committed_at=_OLD,
        payload=json.dumps({"statement": "s1", "fact_type": "ft1"}),
    )])
    assert ff.calls[0]["created_at"] == _OLD_MS


async def test_replay_rule_restores_committed_at():
    ff = await _replay([_commit_row(
        seq=2, object_type="rule", object_id="r1", operation="commit",
        committed_at=_OLD,
        payload=json.dumps({"rule_type": "advisory", "approach": "do y"}),
    )])
    assert ff.calls[0]["created_at"] == _OLD_MS


async def test_replay_without_committed_at_leaves_created_at_unset():
    # No committed_at: the node gets the store's own timestamp.
    ff = await _replay([_commit_row(
        seq=3, object_type="fact", object_id="f2", operation="commit",
        payload=json.dumps({"statement": "s2", "fact_type": "ft2"}),
    )])
    assert ff.calls[0]["created_at"] is None


async def test_replayed_fact_stays_stale():
    """A rebuild keeps an old fact stale."""
    from kwim_api.freshness import _to_dt, compute_freshness

    ff = await _replay([_commit_row(
        seq=1, object_type="fact", object_id="f1", operation="commit",
        committed_at=_OLD,
        payload=json.dumps({"statement": "s1", "fact_type": "observation"}),
    )])
    as_of = _to_dt(ff.calls[0]["created_at"]).isoformat()
    assert compute_freshness(as_of, "slow", 90, 48) == "stale"


async def test_rebuild_semantic_carries_event_time():
    from kwim_api.rebuild import _rebuild_semantic

    events = [{"id": "ev0", "agent_id": "a0", "session_id": "s0", "event_type": "turn",
               "event_data": {"text": "text0"}, "occurred_at": _OLD}]
    ff = _FakeFalkor()
    await _rebuild_semantic(_FakePostgres(episodic_rows=events), ff,
                            _FakeEmbedder([[0.0] * 3]), "acme", None)
    assert ff.calls[0]["created_at"] == _OLD_MS


# ---------------------------------------------------------------------------
# Semantic re-embed batching
# ---------------------------------------------------------------------------

async def test_rebuild_semantic_single_batch():
    from kwim_api.rebuild import _rebuild_semantic

    events = [
        {"id": f"ev{i}", "agent_id": f"a{i}", "session_id": f"s{i}",
         "event_type": "turn", "event_data": {"text": f"text{i}"},
         "occurred_at": "2026-01-01T00:00:00Z"}
        for i in range(5)
    ]
    pg = _FakePostgres(episodic_rows=events)
    ff = _FakeFalkor()
    fe = _FakeEmbedder([[float(i)] * 3 for i in range(5)])

    await _rebuild_semantic(pg, ff, fe, "acme", "kwim_acme_rebuild")

    assert len(fe.calls) == 1  # 5 < batch size
    assert fe.calls[0] == ["text0", "text1", "text2", "text3", "text4"]
    assert len(ff.calls) == 5
    assert ff.calls[0]["graph_name"] == "kwim_acme_rebuild"
    assert ff.calls[2]["item"]["id"] == "ev2"
    assert ff.calls[2]["item"]["embedding"] == [2.0, 2.0, 2.0]
    assert "event_type" in ff.calls[0]["item"]["metadata"]


async def test_rebuild_semantic_multiple_batches():
    from kwim_api.rebuild import _EMBED_BATCH, _rebuild_semantic

    events = [
        {"id": f"ev{i}", "agent_id": "a", "session_id": "s",
         "event_type": "turn", "event_data": {"text": f"t{i}"}, "occurred_at": None}
        for i in range(70)
    ]
    pg = _FakePostgres(episodic_rows=events)
    ff = _FakeFalkor()
    fe = _FakeEmbedder([[float(i)] * 3 for i in range(70)])

    await _rebuild_semantic(pg, ff, fe, "acme", None)

    assert len(fe.calls) == 3  # 70 events, batch 32 -> 32 + 32 + 6
    assert len(fe.calls[0]) == _EMBED_BATCH
    assert len(fe.calls[1]) == _EMBED_BATCH
    assert len(fe.calls[2]) == 70 - 2 * _EMBED_BATCH


# ---------------------------------------------------------------------------
# CLI arg validation
# ---------------------------------------------------------------------------

def test_cli_arg_parsing():

    parser = argparse.ArgumentParser()
    parser.add_argument("--team")
    parser.add_argument("--all-teams", action="store_true")
    parser.add_argument("--skip-semantic", action="store_true")
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--yes", action="store_true")

    ns = parser.parse_args(["--team", "acme", "--skip-semantic", "--yes"])
    assert ns.team == "acme"
    assert ns.skip_semantic
    assert ns.yes
    assert not ns.all_teams

    ns = parser.parse_args(["--all-teams"])
    assert ns.all_teams
    assert ns.team is None
