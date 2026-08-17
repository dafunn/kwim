"""Code distiller refresh behaviour.

Replaces the old object_id/idempotency tests: the distiller now reads the current
fact, compares its statement, and only proposes when the content changes (with
supersedes). It also selects load-bearing functions via PageRank + cross-community
bridging rather than raw fan-in.
"""
import uuid

from kwim_api.codegraph import distill


class _FakeBus:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, team, channel, msg):
        self.published.append(msg)


class _FakeFalkor:
    """Fake store that persists facts so read-diff-supersede can be tested.

    `query_facts` mirrors the real store's case-insensitive ANY match on `about`,
    then distill.py filters client-side to the exact identity ref.
    """

    def __init__(self, hubs=None, ifaces=None, facts=None):
        self._hubs = hubs or []
        self._ifaces = ifaces or []
        self._facts = list(facts or [])
        self.proposal_sets: list[tuple] = []

    async def proposal_set(self, pid, doc):
        self.proposal_sets.append((pid, doc))

    async def code_hubs(self, team, *, repo, min_fan_in, min_confidence):
        return [h for h in self._hubs if h.get("fan_in", 0) >= min_fan_in]

    async def code_cross_repo_interfaces(self, team, *, min_confidence):
        return self._ifaces

    async def query_facts(self, team, fact_type, status, limit, about=None, source_kind=None):
        about_set = {str(a).lower() for a in (about or [])}
        results = []
        for f in self._facts:
            if f.get("status") != status:
                continue
            if fact_type and f.get("fact_type") != fact_type:
                continue
            if source_kind and f.get("source_kind") != source_kind:
                continue
            fact_about = {str(x).lower() for x in (f.get("about") or [])}
            if about_set and not (about_set & fact_about):
                continue
            results.append(dict(f))
            if len(results) >= limit:
                break
        return results

    def apply_commit(self, msg: dict) -> str:
        """Simulate the gate committing a published proposal. Returns the new fact id."""
        body = msg["body"]
        new_id = str(uuid.uuid4())
        if body.get("supersedes"):
            for f in self._facts:
                if f["id"] == body["supersedes"]:
                    f["status"] = "superseded"
        self._facts.append({
            "id": new_id,
            "statement": body["statement"],
            "fact_type": body["fact_type"],
            "status": "current",
            "about": body["about"],
        })
        return new_id


# ---------------------------------------------------------------------------
# Freeze-fix: read-diff-supersede with no object_id
# ---------------------------------------------------------------------------

async def test_unchanged_run_proposes_nothing():
    fk = _FakeFalkor(hubs=[
        {"name": "_graph", "repo": "kwim", "pagerank": 0.027, "fan_in": 6, "bridged": 1},
        {"name": "_key", "repo": "kwim", "pagerank": 0.020, "fan_in": 3, "bridged": 3},
    ])
    bus = _FakeBus()
    await distill.distill_repo(fk, bus, "acme", "kwim", min_fan_in=3)
    assert len(bus.published) == 1
    assert "object_id" not in bus.published[0]["body"]
    fk.apply_commit(bus.published[0])

    # Identical graph: zero proposals.
    bus2 = _FakeBus()
    await distill.distill_repo(fk, bus2, "acme", "kwim", min_fan_in=3)
    assert len(bus2.published) == 0


async def test_changed_hub_set_supersedes():
    fk = _FakeFalkor(hubs=[
        {"name": "_graph", "repo": "kwim", "pagerank": 0.027, "fan_in": 6, "bridged": 1},
    ])
    bus1 = _FakeBus()
    await distill.distill_repo(fk, bus1, "acme", "kwim")
    assert len(bus1.published) == 1
    first_id = fk.apply_commit(bus1.published[0])

    fk._hubs = [
        {"name": "_graph", "repo": "kwim", "pagerank": 0.027, "fan_in": 6, "bridged": 1},
        {"name": "narrate", "repo": "kwim", "pagerank": 0.010, "fan_in": 14, "bridged": 4},
    ]
    bus2 = _FakeBus()
    await distill.distill_repo(fk, bus2, "acme", "kwim")
    assert len(bus2.published) == 1
    body = bus2.published[0]["body"]
    assert body.get("supersedes") == first_id
    assert "object_id" not in body


async def test_new_repo_identity_commits_without_supersedes():
    fk = _FakeFalkor(hubs=[
        {"name": "x", "repo": "alpha", "pagerank": 0.1, "fan_in": 5, "bridged": 1},
    ])
    bus = _FakeBus()
    await distill.distill_repo(fk, bus, "acme", "alpha")
    assert len(bus.published) == 1
    body = bus.published[0]["body"]
    assert "supersedes" not in body
    assert "object_id" not in body


async def test_interface_identity_disambiguated_by_path():
    fk = _FakeFalkor(ifaces=[
        {"id": "i1", "name": "load", "repo": "alpha", "path": "a/load.py", "consumer_repos": ["beta"]},
        {"id": "i2", "name": "load", "repo": "alpha", "path": "b/load.py", "consumer_repos": ["gamma"]},
    ])
    bus = _FakeBus()
    await distill.distill_repo(fk, bus, "acme", "alpha")
    assert len(bus.published) == 2
    abouts = [tuple(m["body"]["about"]) for m in bus.published]
    assert len(set(abouts)) == 2
    for m in bus.published:
        assert "object_id" not in m["body"]
        fk.apply_commit(m)

    # Re-run unchanged: no proposals.
    bus2 = _FakeBus()
    await distill.distill_repo(fk, bus2, "acme", "alpha")
    assert len(bus2.published) == 0


# ---------------------------------------------------------------------------
# Metric upgrade: PageRank + bridging + alphabetical set statement
# ---------------------------------------------------------------------------

async def test_metric_noise_floor_drops_low_fan_in():
    fk = _FakeFalkor(hubs=[
        {"name": "__init__", "repo": "kwim", "pagerank": 0.050, "fan_in": 1, "bridged": 1},
        {"name": "_graph", "repo": "kwim", "pagerank": 0.027, "fan_in": 6, "bridged": 1},
    ])
    bus = _FakeBus()
    await distill.distill_repo(fk, bus, "acme", "kwim", min_fan_in=5)
    assert len(bus.published) == 1
    statement = bus.published[0]["body"]["statement"]
    assert "_graph" in statement
    assert "__init__" not in statement


async def test_metric_bridging_rescues_low_call_seam():
    fk = _FakeFalkor(hubs=[
        {"name": "narrate", "repo": "kwim", "pagerank": 0.030, "fan_in": 14, "bridged": 1},
        {"name": "_key", "repo": "kwim", "pagerank": 0.010, "fan_in": 3, "bridged": 3},
    ])
    bus = _FakeBus()
    await distill.distill_repo(fk, bus, "acme", "kwim", min_fan_in=3)
    assert len(bus.published) == 1
    statement = bus.published[0]["body"]["statement"]
    assert "narrate" in statement   # top by PageRank
    assert "_key" in statement      # rescued by cross-community bridging


async def test_metric_statement_has_no_numbers_and_is_order_stable():
    fk = _FakeFalkor(hubs=[
        {"name": "z_fn", "repo": "kwim", "pagerank": 0.030, "fan_in": 10, "bridged": 1},
        {"name": "a_fn", "repo": "kwim", "pagerank": 0.010, "fan_in": 10, "bridged": 1},
    ])
    bus = _FakeBus()
    await distill.distill_repo(fk, bus, "acme", "kwim")
    assert len(bus.published) == 1
    statement = bus.published[0]["body"]["statement"]
    assert "10" not in statement
    assert "(10" not in statement
    assert statement.index("a_fn") < statement.index("z_fn")
    fk.apply_commit(bus.published[0])

    # Same set, reversed PageRank ranks -> alphabetical statement unchanged -> no proposal.
    fk._hubs = [
        {"name": "a_fn", "repo": "kwim", "pagerank": 0.030, "fan_in": 10, "bridged": 1},
        {"name": "z_fn", "repo": "kwim", "pagerank": 0.010, "fan_in": 10, "bridged": 1},
    ]
    bus2 = _FakeBus()
    await distill.distill_repo(fk, bus2, "acme", "kwim")
    assert len(bus2.published) == 0
