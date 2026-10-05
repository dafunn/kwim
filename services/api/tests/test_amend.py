"""In-place editing of committed objects (commit_log operation 'amend').

Amend overwrites content fields only, never status, created_at or edges. See
docs/DESIGN.md, "Changing committed objects".
"""
import json

import pytest

from kwim_api.gate import Gate

_FACT = {"statement": "original", "fact_type": "observation", "about": ["x"],
         "decay_class": "slow", "status": "current"}
_RULE = {"situation": {"task_type": "draft"}, "approach": "old approach",
         "action_pattern": None, "verdict": None, "authority": None,
         "severity": None, "check_tier": None, "status": "approved",
         "rule_type": "advisory"}
_SEM = {"content": "original chunk", "metadata": {"doc_type": "runbook"}}


class _FakeFalkor:
    AMENDABLE = {
        "fact": ("statement", "fact_type", "about", "decay_class"),
        "rule": ("situation", "approach", "action_pattern", "verdict", "authority",
                 "severity", "check_tier"),
        "semantic": ("content", "metadata"),
    }

    def __init__(self, fact=None, rule=None, semantic=None, applies=True):
        self._fact, self._rule, self._sem = fact, rule, semantic
        self._applies = applies
        self.calls: list[dict] = []

    async def get_fact_content(self, team, fid):
        return dict(self._fact) if self._fact else None

    async def get_rule_content(self, team, rid):
        return dict(self._rule) if self._rule else None

    async def get_semantic_content(self, team, sid):
        return dict(self._sem) if self._sem else None

    async def amend_fact(self, team, fid, payload, seq, graph_name=None, embedding=None):
        self.calls.append({"m": "amend_fact", "id": fid, "payload": payload,
                           "seq": seq, "embedding": embedding})
        return self._applies

    async def amend_rule(self, team, rid, payload, seq, previous_situation=None,
                         graph_name=None):
        self.calls.append({"m": "amend_rule", "id": rid, "payload": payload,
                           "seq": seq, "previous_situation": previous_situation})
        return self._applies

    async def amend_semantic(self, team, sid, payload, previous_metadata=None,
                             graph_name=None, embedding=None):
        self.calls.append({"m": "amend_semantic", "id": sid, "payload": payload,
                           "previous_metadata": previous_metadata,
                           "embedding": embedding})
        return self._applies


class _FakePg:
    def __init__(self):
        self.rows: list[dict] = []

    async def append_commit(self, team, row):
        self.rows.append(row)
        return len(self.rows)


class _FakeEmbedder:
    async def embed(self, texts):
        return [[0.25] * 3 for _ in texts]


def _gate(**kw):
    fk, pg = _FakeFalkor(**kw), _FakePg()
    return Gate(pg, fk, None, _FakeEmbedder()), fk, pg


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

async def test_amend_unknown_object_writes_no_row():
    gate, fk, pg = _gate(fact=None)
    r = await gate.amend_object("acme", "nope", "fact", {"statement": "x"}, "op", "api")
    assert r["status"] == "not_found"
    assert pg.rows == []
    assert fk.calls == []


async def test_amend_superseded_fact_is_refused():
    gate, fk, pg = _gate(fact={**_FACT, "status": "superseded"})
    r = await gate.amend_object("acme", "f1", "fact", {"statement": "x"}, "op", "api")
    assert r["status"] == "not_current"
    assert r["object_status"] == "superseded"
    assert pg.rows == []
    assert fk.calls == []


async def test_amend_retracted_fact_is_refused():
    gate, _, pg = _gate(fact={**_FACT, "status": "retracted"})
    r = await gate.amend_object("acme", "f1", "fact", {"statement": "x"}, "op", "api")
    assert r["status"] == "not_current"
    assert pg.rows == []


async def test_amend_deprecated_rule_is_refused():
    gate, _, pg = _gate(rule={**_RULE, "status": "deprecated"})
    r = await gate.amend_object("acme", "r1", "rule", {"approach": "x"}, "op", "api")
    assert r["status"] == "not_current"
    assert pg.rows == []


@pytest.mark.parametrize("field", ["status", "created_at", "id", "commit_seq",
                                   "evidence_count", "scope"])
async def test_amend_refuses_non_content_fields(field):
    """Status, identity, and lineage each have their own operation."""
    gate, fk, pg = _gate(fact=dict(_FACT))
    r = await gate.amend_object("acme", "f1", "fact", {field: "x"}, "op", "api")
    assert r["status"] == "invalid_field"
    assert field in r["fields"]
    assert pg.rows == []
    assert fk.calls == []


async def test_amend_empty_payload_is_refused():
    gate, _, pg = _gate(fact=dict(_FACT))
    r = await gate.amend_object("acme", "f1", "fact", {}, "op", "api")
    assert r["status"] == "invalid_field"
    assert pg.rows == []


async def test_amend_unknown_object_type_is_refused():
    gate, _, pg = _gate(fact=dict(_FACT))
    r = await gate.amend_object("acme", "x", "widget", {"statement": "x"}, "op", "api")
    assert r["status"] == "invalid_field"
    assert pg.rows == []


# ---------------------------------------------------------------------------
# The log row
# ---------------------------------------------------------------------------

async def test_amend_logs_previous_payload():
    """The node is overwritten, so the row is the only record of the old text."""
    gate, _, pg = _gate(fact=dict(_FACT))
    r = await gate.amend_object("acme", "f1", "fact", {"statement": "corrected"},
                                "operator-a", "admin_console")
    assert r["status"] == "amended"
    assert len(pg.rows) == 1
    row = pg.rows[0]
    assert row["operation"] == "amend"
    assert row["object_type"] == "fact"
    assert row["payload"] == {"statement": "corrected"}
    assert row["provenance"]["previous_payload"] == {"statement": "original"}
    assert row["provenance"]["amended_by"] == "operator-a"
    assert row["provenance"]["amended_via"] == "admin_console"


async def test_amend_logs_the_reason_in_provenance():
    """The governance log carries why, not just what and who - the console's own
    audit table is not the governance record."""
    gate, _, pg = _gate(fact=dict(_FACT))
    await gate.amend_object("acme", "f1", "fact", {"statement": "x"}, "op", "api",
                            reason="fixed a typo in the hostname")
    assert pg.rows[0]["provenance"]["reason"] == "fixed a typo in the hostname"


async def test_amend_without_a_reason_omits_the_key():
    """gate.amend_object is callable without one; the console endpoint requires it."""
    gate, _, pg = _gate(fact=dict(_FACT))
    await gate.amend_object("acme", "f1", "fact", {"statement": "x"}, "op", "api")
    assert "reason" not in pg.rows[0]["provenance"]


async def test_previous_payload_covers_only_amended_fields():
    gate, _, pg = _gate(fact=dict(_FACT))
    await gate.amend_object("acme", "f1", "fact", {"about": ["y"]}, "op", "api")
    assert pg.rows[0]["provenance"]["previous_payload"] == {"about": ["x"]}


async def test_amend_logs_before_touching_the_graph():
    gate, fk, pg = _gate(fact=dict(_FACT))
    await gate.amend_object("acme", "f1", "fact", {"statement": "x"}, "op", "api")
    assert len(pg.rows) == 1 and len(fk.calls) == 1
    assert fk.calls[0]["seq"] == 1, "the graph write carries the log row's seq"


async def test_amend_survives_a_failed_graph_write():
    """A row with no node change is repaired by the next rebuild; the reverse is not."""
    gate, fk, pg = _gate(fact=dict(_FACT), applies=False)
    r = await gate.amend_object("acme", "f1", "fact", {"statement": "x"}, "op", "api")
    assert r["status"] == "amended"
    assert len(pg.rows) == 1


# ---------------------------------------------------------------------------
# Per-type behaviour
# ---------------------------------------------------------------------------

async def test_amending_a_statement_re_embeds():
    """A stale vector would keep matching the old text in semantic search."""
    gate, fk, _ = _gate(fact=dict(_FACT))
    await gate.amend_object("acme", "f1", "fact", {"statement": "new"}, "op", "api")
    assert fk.calls[0]["embedding"] == [0.25, 0.25, 0.25]


async def test_amending_only_metadata_does_not_re_embed():
    gate, fk, _ = _gate(fact=dict(_FACT))
    await gate.amend_object("acme", "f1", "fact", {"about": ["y"]}, "op", "api")
    assert fk.calls[0]["embedding"] is None


async def test_amend_rule_passes_previous_situation():
    """Stale promoted keys would keep matching in query_rules; the store removes
    them using the old situation."""
    gate, fk, _ = _gate(rule=dict(_RULE))
    await gate.amend_object("acme", "r1", "rule",
                            {"situation": {"platform": "web"}}, "op", "api")
    assert fk.calls[0]["previous_situation"] == {"task_type": "draft"}


async def test_amend_semantic_passes_previous_metadata_and_re_embeds():
    gate, fk, _ = _gate(semantic=dict(_SEM))
    await gate.amend_object("acme", "s1", "semantic",
                            {"content": "new chunk", "metadata": {"doc_type": "spec"}},
                            "op", "api")
    assert fk.calls[0]["previous_metadata"] == {"doc_type": "runbook"}
    assert fk.calls[0]["embedding"] == [0.25, 0.25, 0.25]


async def test_amend_semantic_has_no_currency_guard():
    """Semantic items carry no status, so there is nothing to be non-current."""
    gate, _, pg = _gate(semantic=dict(_SEM))
    r = await gate.amend_object("acme", "s1", "semantic", {"content": "x"}, "op", "api")
    assert r["status"] == "amended"
    assert len(pg.rows) == 1


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

class _ReplayFalkor(_FakeFalkor):
    def __init__(self):
        super().__init__()

    async def materialize_fact(self, team, fact, provenance, graph_name=None,
                               embedding=None, created_at=None):
        self.calls.append({"m": "materialize_fact", "fact": fact})

    async def retract_object(self, team, object_type, object_id, graph_name=None):
        self.calls.append({"m": "retract_object", "id": object_id})


class _ReplayPg:
    def __init__(self, rows):
        self.rows = rows

    async def replay_commit_log(self, team):
        return self.rows


def _row(seq, obj_type, obj_id, op, payload=None, provenance=None):
    return {"seq": seq, "object_type": obj_type, "object_id": obj_id,
            "operation": op, "payload": json.dumps(payload or {}),
            "provenance": json.dumps(provenance or {})}


async def test_replay_applies_amend_after_commit():
    from kwim_api.rebuild import _replay_commit

    fk = _ReplayFalkor()
    rows = [
        _row(1, "fact", "f1", "commit", {"statement": "original", "fact_type": "ft"}),
        _row(2, "fact", "f1", "amend", {"statement": "corrected"},
             {"previous_payload": {"statement": "original"}}),
    ]
    await _replay_commit(_ReplayPg(rows), fk, "acme", None, _FakeEmbedder())
    assert [c["m"] for c in fk.calls] == ["materialize_fact", "amend_fact"]
    assert fk.calls[1]["payload"] == {"statement": "corrected"}
    assert fk.calls[1]["seq"] == 2


async def test_replay_amend_then_retract_keeps_both():
    """Amend must not touch status, so a later retract still lands."""
    from kwim_api.rebuild import _replay_commit

    fk = _ReplayFalkor()
    rows = [
        _row(1, "fact", "f1", "commit", {"statement": "original", "fact_type": "ft"}),
        _row(2, "fact", "f1", "amend", {"statement": "corrected"}),
        _row(3, "fact", "f1", "retract"),
    ]
    await _replay_commit(_ReplayPg(rows), fk, "acme", None, _FakeEmbedder())
    assert [c["m"] for c in fk.calls] == ["materialize_fact", "amend_fact",
                                          "retract_object"]


async def test_replay_amend_rule_uses_logged_previous_situation():
    from kwim_api.rebuild import _replay_commit

    fk = _ReplayFalkor()
    rows = [_row(1, "rule", "r1", "amend", {"situation": {"platform": "web"}},
                 {"previous_payload": {"situation": {"task_type": "draft"}}})]
    await _replay_commit(_ReplayPg(rows), fk, "acme", None, _FakeEmbedder())
    assert fk.calls[0]["previous_situation"] == {"task_type": "draft"}


async def test_replay_amend_semantic_re_embeds():
    from kwim_api.rebuild import _replay_commit

    fk = _ReplayFalkor()
    rows = [_row(1, "semantic", "s1", "amend", {"content": "new"})]
    await _replay_commit(_ReplayPg(rows), fk, "acme", None, _FakeEmbedder())
    assert fk.calls[0]["m"] == "amend_semantic"
    assert fk.calls[0]["embedding"] == [0.25, 0.25, 0.25]


async def test_replay_last_amend_wins():
    from kwim_api.rebuild import _replay_commit

    fk = _ReplayFalkor()
    rows = [
        _row(1, "fact", "f1", "commit", {"statement": "v1", "fact_type": "ft"}),
        _row(2, "fact", "f1", "amend", {"statement": "v2"}),
        _row(3, "fact", "f1", "amend", {"statement": "v3"}),
    ]
    await _replay_commit(_ReplayPg(rows), fk, "acme", None, _FakeEmbedder())
    assert fk.calls[-1]["payload"] == {"statement": "v3"}
