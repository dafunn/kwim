"""Which files the extractor rewrites (and so re-embeds) on a run.

An unchanged file is normally skipped, since embedding is the costly step. A file
whose functions have no embeddings is rewritten anyway, so a failed embed call
on an earlier run is repaired by the next one. Driven with a fake store and
embedder over a real checkout directory.
"""
from __future__ import annotations

from kwim_api.codegraph import extract, incremental

_SOURCE = b"def alpha():\n    return 1\n\n\ndef beta():\n    return alpha()\n"


class _FakeStore:
    def __init__(self, hashes: dict[str, str], unembedded: set[str]):
        self._hashes = hashes
        self._unembedded = unembedded
        self.functions: list[dict] = []

    async def code_files_by_hash(self, team, repo):
        return dict(self._hashes)

    async def code_files_missing_embeddings(self, team, repo):
        return set(self._unembedded)

    async def materialize_code_function(self, team, **kw):
        self.functions.append(kw)

    async def prune_repo_files(self, team, *, repo, keep_paths):
        return 0

    async def _noop(self, *a, **kw):
        return None

    materialize_code_file = _noop
    materialize_code_class = _noop
    materialize_import_edge = _noop
    materialize_call_edge = _noop
    set_member_of = _noop


class _FakeEmbedder:
    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [[0.1] for _ in texts]


def _checkout(tmp_path):
    (tmp_path / "mod.py").write_bytes(_SOURCE)
    return str(tmp_path)


async def test_unchanged_embedded_file_is_not_rewritten(tmp_path):
    repo_dir = _checkout(tmp_path)
    store = _FakeStore({"mod.py": incremental.content_hash(_SOURCE)}, set())
    emb = _FakeEmbedder()
    await extract.extract_repo(store, "team_a", "demo", repo_dir, embedder=emb)
    assert emb.calls == 0
    assert store.functions == []


async def test_unchanged_file_missing_embeddings_is_re_embedded(tmp_path):
    repo_dir = _checkout(tmp_path)
    store = _FakeStore({"mod.py": incremental.content_hash(_SOURCE)}, {"mod.py"})
    emb = _FakeEmbedder()
    summary = await extract.extract_repo(store, "team_a", "demo", repo_dir, embedder=emb)
    assert emb.calls == 1
    assert summary["definitions"] == 2
    assert all(f["embedding"] == [0.1] for f in store.functions)


async def test_without_an_embedder_missing_embeddings_are_not_looked_up(tmp_path):
    repo_dir = _checkout(tmp_path)
    store = _FakeStore({"mod.py": incremental.content_hash(_SOURCE)}, {"mod.py"})
    await extract.extract_repo(store, "team_a", "demo", repo_dir, embedder=None)
    assert store.functions == []
