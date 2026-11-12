"""The rebuild's swap and catch-up, with a team that keeps taking writes: the
swap runs under the team's live-graph lock, a failed copy restores the live
graph from its backup, and rows committed during the rebuild are replayed
afterwards."""
import asyncio
import json

import pytest

from kwim_api import rebuild
from kwim_api.stores.falkor import FalkorStore, live_graph_lock


class _Result:
    def __init__(self, n):
        self.result_set = [[n]]


class _Graph:
    def __init__(self, db, name):
        self.db, self.name = db, name

    async def query(self, q, *a, **k):
        return _Result(len(self.db.nodes.get(self.name, set())))


class _Conn:
    """GRAPH.DELETE / GRAPH.COPY over a dict of graph name -> node ids."""

    def __init__(self, db):
        self.db = db
        self.log: list[tuple] = []

    async def execute_command(self, *args):
        self.log.append(args)
        cmd = args[0]
        if cmd == "GRAPH.DELETE":
            if args[1] not in self.db.nodes:
                raise Exception("Invalid graph operation on empty key")
            del self.db.nodes[args[1]]
            return "OK"
        if cmd == "GRAPH.COPY":
            if self.db.no_copy:
                raise Exception("ERR unknown command 'GRAPH.COPY'")
            src, dst = args[1], args[2]
            if src not in self.db.nodes:
                raise Exception("Invalid graph operation on empty key")
            if dst in self.db.nodes or src in self.db.fail_copy_from:
                raise Exception("copy failed")
            self.db.nodes[dst] = set(self.db.nodes[src])
            return "OK"
        raise AssertionError(args)


class _DB:
    def __init__(self, nodes, fail_copy_from=(), no_copy=False):
        self.nodes = {k: set(v) for k, v in nodes.items()}
        self.fail_copy_from = set(fail_copy_from)
        self.no_copy = no_copy
        self.connection = _Conn(self)

    def select_graph(self, name):
        return _Graph(self, name)


class _Falkor:
    def __init__(self, db):
        self._db = db
        self._inited: set[str] = set()
        self.materialized: list[tuple[str, str | None]] = []

    async def materialize_fact(self, team, fact, provenance, graph_name=None, embedding=None,
                               created_at=None):
        name = graph_name or f"kwim_{team}"
        self._db.nodes.setdefault(name, set()).add(fact["id"])
        self.materialized.append((fact["id"], graph_name))

    async def _graph(self, team):
        return self._db.select_graph(f"kwim_{team}")


class _Pg:
    """commit_log rows; `arrive` rows appear once the first replay has read."""

    def __init__(self, rows, arrive=()):
        self.rows, self.arrive = list(rows), list(arrive)
        self.reads = 0

    async def replay_commit_log(self, team, since_seq=None, until_seq=None):
        self.reads += 1
        if self.reads == 2 and self.arrive:  # writes during the rebuild
            self.rows += self.arrive
        return [r for r in self.rows
                if (since_seq is None or r["seq"] > since_seq)
                and (until_seq is None or r["seq"] <= until_seq)]

    async def read_verifications(self, team):
        return []


def _fact(seq, oid):
    return {"seq": seq, "object_type": "fact", "object_id": oid, "operation": "commit",
            "payload": json.dumps({"statement": oid}), "provenance": json.dumps({})}


async def test_swap_replaces_live_and_cleans_up():
    db = _DB({"kwim_acme": {"old"}, "kwim_acme_rebuild": {"a", "b"}})
    assert await rebuild._swap_graphs(_Falkor(db), "acme") is True
    assert db.nodes == {"kwim_acme": {"a", "b"}}


async def test_swap_with_no_live_graph_yet():
    db = _DB({"kwim_acme_rebuild": {"a"}})
    assert await rebuild._swap_graphs(_Falkor(db), "acme") is True
    assert db.nodes == {"kwim_acme": {"a"}}


async def test_a_failed_copy_restores_live_and_keeps_the_rebuilt_graph():
    db = _DB({"kwim_acme": {"old"}, "kwim_acme_rebuild": {"a"}}, fail_copy_from={"kwim_acme_rebuild"})
    assert await rebuild._swap_graphs(_Falkor(db), "acme") is False
    assert db.nodes["kwim_acme"] == {"old"}
    assert db.nodes["kwim_acme_rebuild"] == {"a"}
    assert "kwim_acme_prerebuild" not in db.nodes


class _RecGraph:
    def __init__(self, name, log):
        self.name, self.log = name, log

    async def query(self, q, *a, **k):
        self.log.append((self.name, q))


class _RecDB:
    def __init__(self):
        self.log: list[tuple[str, str]] = []

    def select_graph(self, name):
        return _RecGraph(name, self.log)


async def test_live_graph_queries_wait_for_the_swap_lock_and_temp_queries_do_not():
    """Reads too: in FalkorDB a query on a missing key creates the graph."""
    db = _RecDB()
    store = FalkorStore()
    store._db = db
    store._inited = {"kwim_acme", "kwim_acme_rebuild"}
    live = await store._graph("acme")
    temp = await store._graph("acme", "kwim_acme_rebuild")
    async with live_graph_lock("acme"):
        read = asyncio.create_task(live.query("MATCH (f:Fact) RETURN f"))
        await temp.query("MERGE (f:Fact {id:'t'})")
        await asyncio.sleep(0.05)
        assert db.log == [("kwim_acme_rebuild", "MERGE (f:Fact {id:'t'})")]
    await read
    assert db.log[-1] == ("kwim_acme", "MATCH (f:Fact) RETURN f")


async def test_schema_setup_on_the_live_graph_waits_for_the_lock():
    db = _RecDB()
    store = FalkorStore()
    store._db = db
    async with live_graph_lock("acme"):
        task = asyncio.create_task(store._graph("acme"))
        await asyncio.sleep(0.05)
        assert db.log == []          # CREATE INDEX would recreate a deleted graph
    await task
    assert db.log and all(name == "kwim_acme" for name, _ in db.log)


async def test_no_graph_copy_raises_swap_unsupported():
    db = _DB({"kwim_acme": {"old"}, "kwim_acme_rebuild": {"a"}}, no_copy=True)
    with pytest.raises(rebuild.SwapUnsupported):
        await rebuild._swap_graphs(_Falkor(db), "acme")


async def test_rebuild_under_writes_converges():
    """Rows committed during the replay are not in the sanity count, and are
    replayed into the live graph after the swap."""
    db = _DB({"kwim_acme": {"f1"}, "kwim_acme_rebuild": {"stale"}})
    falkor = _Falkor(db)
    pg = _Pg([_fact(1, "f1"), _fact(2, "f2")], arrive=[_fact(3, "f3"), _fact(4, "f4")])
    ok = await rebuild.rebuild_team("acme", pg, falkor, None, in_place=False, yes=True)
    assert ok is True
    assert db.nodes["kwim_acme"] == {"f1", "f2", "f3", "f4"}
    assert "kwim_acme_rebuild" not in db.nodes
    # The stale temp graph was dropped first, so nothing from it survived.
    assert "stale" not in db.nodes["kwim_acme"]
    assert ("f3", None) in falkor.materialized and ("f4", None) in falkor.materialized
