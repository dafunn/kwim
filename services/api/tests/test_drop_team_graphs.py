"""Team destroy's graph drop: a graph that is not there is reported, not raised,
whichever way FalkorDB says so."""
import pytest

from kwim_api.stores.falkor import FalkorStore


class _Graph:
    def __init__(self, db, name):
        self.db, self.name = db, name

    async def delete(self):
        if self.name in self.db.vanish:
            raise Exception("Invalid graph operation on empty key")
        self.db.deleted.append(self.name)


class _Conn:
    def __init__(self, graphs):
        self.graphs = graphs

    async def execute_command(self, *args):
        assert args == ("GRAPH.LIST",)
        return [g.encode() for g in self.graphs]


class _DB:
    def __init__(self, graphs, vanish=()):
        self.connection = _Conn(graphs)
        self.vanish, self.deleted = set(vanish), []

    def select_graph(self, name):
        return _Graph(self, name)


def _store(db):
    store = FalkorStore()
    store._db = db
    store._inited = {"kwim_acme", "kwim_acme_code", "kwim_acme_docs"}
    return store


async def test_a_team_with_no_code_graph_drops_its_main_graph():
    db = _DB(["kwim_acme", "kwim_other"])
    store = _store(db)
    assert await store.drop_team_graphs("acme") == {
        "kwim_acme": True, "kwim_acme_code": False, "kwim_acme_docs": False}
    assert db.deleted == ["kwim_acme"]
    assert not store._inited


async def test_a_team_with_documents_drops_its_docs_graph():
    db = _DB(["kwim_acme", "kwim_acme_docs"])
    assert await _store(db).drop_team_graphs("acme") == {
        "kwim_acme": True, "kwim_acme_code": False, "kwim_acme_docs": True}
    assert db.deleted == ["kwim_acme", "kwim_acme_docs"]


async def test_a_graph_gone_between_list_and_delete_is_absent():
    db = _DB(["kwim_acme", "kwim_acme_code"], vanish={"kwim_acme_code"})
    assert await _store(db).drop_team_graphs("acme") == {
        "kwim_acme": True, "kwim_acme_code": False, "kwim_acme_docs": False}


async def test_any_other_error_is_raised():
    class _Broken(_DB):
        def select_graph(self, name):
            class G:
                async def delete(self_inner):
                    raise Exception("connection reset")
            return G()

    with pytest.raises(Exception, match="connection reset"):
        await _store(_Broken(["kwim_acme"])).drop_team_graphs("acme")
