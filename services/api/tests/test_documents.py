"""Tests for document upload, storage, the ingest job skeleton and revision
retention.
"""
import hashlib
import os
import uuid

import pytest

from kwim_api.config import settings
from kwim_api.documents import ingest
from kwim_api.documents.storage import DocumentStorage, UploadTooLarge
from tests.test_admin_read import admin_enabled  # noqa: F401 - fixture reused by name
from tests.test_admin_write import _NOW
from tests.test_admin_write import FakePgStore as _BaseFakePgStore

PDF = b"%PDF-1.7\n" + b"x" * 2000


class FakePgStore(_BaseFakePgStore):
    """Adds the document tables and append_commit."""

    def __init__(self):
        super().__init__()
        self.documents: dict[str, dict] = {}
        self.revisions: dict[str, dict] = {}
        self.commits: list[dict] = []

    async def append_commit(self, team, row):
        self.commits.append({"team": team, **row})
        return len(self.commits)

    async def create_document(self, team, *, title, tags, created_by):
        doc = {"id": str(uuid.uuid4()), "team": team, "title": title, "tags": tags,
               "status": "ingesting", "current_revision": None, "created_at": _NOW,
               "created_by": created_by, "updated_at": _NOW}
        self.documents[doc["id"]] = doc
        return dict(doc)

    async def add_revision(self, team, *, document_id, sha256, mime_type, filename,
                           byte_size, label, uploaded_by):
        n = 1 + max((r["revision_number"] for r in self.revisions.values()
                     if r["document_id"] == document_id), default=0)
        rev = {"id": str(uuid.uuid4()), "team": team, "document_id": document_id,
               "revision_number": n, "label": label, "sha256": sha256,
               "mime_type": mime_type, "filename": filename, "byte_size": byte_size,
               "page_count": None, "extractor": None, "extractor_version": None,
               "extracted": None, "status": "ingesting", "error": None,
               "uploaded_at": _NOW, "uploaded_by": uploaded_by, "ingested_at": None}
        self.revisions[rev["id"]] = rev
        return dict(rev)

    async def get_document(self, team, document_id):
        doc = self.documents.get(document_id)
        if doc is None or doc["team"] != team:
            return None
        revs = sorted((dict(r) for r in self.revisions.values()
                       if r["document_id"] == document_id),
                      key=lambda r: r["revision_number"], reverse=True)
        return {**doc, "revisions": revs}

    async def get_revision(self, team, revision_id):
        rev = self.revisions.get(revision_id)
        return dict(rev) if rev and rev["team"] == team else None

    async def list_documents(self, team, *, limit=100, offset=0):
        docs = [dict(d) for d in self.documents.values() if d["team"] == team]
        return docs[offset:offset + limit]

    async def finish_revision(self, team, revision_id, *, status, error=None,
                              extracted=None, extractor=None, extractor_version=None,
                              page_count=None):
        rev = self.revisions[revision_id]
        rev.update(status=status, error=error)
        if extracted is not None:
            rev.update(extracted=extracted, extractor=extractor,
                       extractor_version=extractor_version, page_count=page_count)
        doc = self.documents[rev["document_id"]]
        if status == "ready":
            doc["current_revision"] = revision_id
            if doc["status"] != "retracted":
                doc["status"] = "ready"
        elif doc["current_revision"] is None and doc["status"] == "ingesting":
            doc["status"] = "failed"

    async def set_document_ingesting(self, team, document_id):
        doc = self.documents[document_id]
        if doc["status"] == "failed":
            doc["status"] = "ingesting"

    async def prune_revisions(self, team, document_id, keep):
        doc = self.documents[document_id]
        revs = sorted((r for r in self.revisions.values() if r["document_id"] == document_id),
                      key=lambda r: r["revision_number"], reverse=True)
        keep_ids = {r["id"] for r in revs[:max(keep, 1)]} | {doc["current_revision"]}
        removed = [r for r in revs if r["id"] not in keep_ids]
        for r in removed:
            del self.revisions[r["id"]]
        return [r["sha256"] for r in removed]

    async def replace_figures(self, team, revision_id, figures):
        self.revisions[revision_id]["figures"] = list(figures)

    async def sha256_in_use(self, team, sha256):
        return any(r["sha256"] == sha256 and r["team"] == team
                   for r in self.revisions.values())


@pytest.fixture
def stores(monkeypatch):
    from kwim_api.runtime import State
    from tests.test_admin_write import FakeAdminStore, FakeFalkorStore

    admin, pg, falkor = FakeAdminStore(), FakePgStore(), FakeFalkorStore()
    monkeypatch.setattr(State, "admin", admin, raising=False)
    monkeypatch.setattr(State, "pg", pg, raising=False)
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    return admin, pg, falkor


@pytest.fixture
def operator_headers(stores):
    admin, _, _ = stores
    token = admin.seed_session(operator_id=admin.seed_operator(username="alice"))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def team_key_headers(stores):
    from kwim_api import auth as auth_mod
    from kwim_api.keys import generate_key

    full_key, _, _ = generate_key()
    auth_mod._KEY_MAP[full_key] = "acme"
    return {"Authorization": f"Bearer {full_key}"}


@pytest.fixture
def acme(stores):
    stores[1].seed_schema("acme")
    return "acme"


@pytest.fixture
def doc_root(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    object.__setattr__(settings, "documents_storage_path", str(root))
    yield root
    object.__setattr__(settings, "documents_storage_path", "/data/documents")


@pytest.fixture
def no_ingest(monkeypatch):
    """Record ingest launches instead of running them."""
    launched: list[tuple] = []

    async def _record(*args):
        launched.append(args)

    monkeypatch.setattr(ingest, "run_ingest", _record)
    return launched


pytestmark = pytest.mark.usefixtures("admin_enabled")


def _upload(client, headers, body=PDF, ctype="application/pdf", **params):
    params.setdefault("filename", "manual.pdf")
    return client.post("/v1/admin/teams/acme/documents", content=body, params=params,
                       headers={**headers, "Content-Type": ctype})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

async def _chunks(data: bytes, size: int = 512):
    for i in range(0, len(data), size):
        yield data[i:i + size]


async def test_storage_saves_under_sha256_and_leaves_no_temp_file(tmp_path):
    store = DocumentStorage(str(tmp_path))
    sha, size = await store.save_original("acme", _chunks(PDF), max_bytes=10_000)
    assert sha == hashlib.sha256(PDF).hexdigest()
    assert size == len(PDF)
    with open(store.original_path("acme", sha), "rb") as fh:
        assert fh.read() == PDF
    assert os.listdir(tmp_path / "acme" / "tmp") == []


async def test_storage_identical_upload_keeps_one_file(tmp_path):
    store = DocumentStorage(str(tmp_path))
    first, _ = await store.save_original("acme", _chunks(PDF), max_bytes=10_000)
    second, _ = await store.save_original("acme", _chunks(PDF), max_bytes=10_000)
    assert first == second
    assert os.listdir(tmp_path / "acme" / "originals") == [first]
    assert os.listdir(tmp_path / "acme" / "tmp") == []


async def test_storage_over_the_limit_raises_and_stores_nothing(tmp_path):
    store = DocumentStorage(str(tmp_path))
    with pytest.raises(UploadTooLarge):
        await store.save_original("acme", _chunks(PDF), max_bytes=100)
    assert os.listdir(tmp_path / "acme" / "tmp") == []
    assert not (tmp_path / "acme" / "originals").exists()


def test_storage_rejects_unsafe_team_and_digest(tmp_path):
    store = DocumentStorage(str(tmp_path))
    with pytest.raises(ValueError):
        store.original_path("../etc", "a" * 64)
    with pytest.raises(ValueError):
        store.original_path("acme", "../../passwd")


async def test_storage_delete_reports_whether_the_file_existed(tmp_path):
    store = DocumentStorage(str(tmp_path))
    sha, _ = await store.save_original("acme", _chunks(PDF), max_bytes=10_000)
    assert await store.delete_original("acme", sha) is True
    assert await store.delete_original("acme", sha) is False


# ---------------------------------------------------------------------------
# Upload route
# ---------------------------------------------------------------------------

def test_upload_stores_the_file_and_records_document_revision_commit_and_job(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    admin, pg, _ = stores
    r = _upload(client, operator_headers, title="Pump manual", tags="pumps, fuel")
    assert r.status_code == 202, r.text
    body = r.json()
    sha = hashlib.sha256(PDF).hexdigest()
    assert body["sha256"] == sha and body["byte_size"] == len(PDF)
    assert body["revision_number"] == 1
    assert (doc_root / "acme" / "originals" / sha).is_file()

    doc = pg.documents[body["document_id"]]
    assert doc["title"] == "Pump manual" and doc["tags"] == ["pumps", "fuel"]
    assert doc["created_by"] == "alice"
    rev = pg.revisions[body["revision_id"]]
    assert rev["mime_type"] == "application/pdf" and rev["filename"] == "manual.pdf"

    commit = pg.commits[0]
    assert commit["object_type"] == "document" and commit["operation"] == "commit"
    assert commit["object_id"] == body["document_id"]
    assert commit["payload"]["sha256"] == sha
    assert commit["gate_decision"] == "human_approved"

    job = admin.jobs[0]
    assert job["id"] == body["job_id"] and job["kind"] == "document_ingest"
    assert job["status"] == "running"
    assert no_ingest == [(body["job_id"], "acme", body["document_id"], body["revision_id"])]
    assert admin.audit_rows[-1]["action"] == "document.upload"
    assert admin.audit_rows[-1]["result"] == "ok"


def test_upload_title_defaults_to_the_filename(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    _, pg, _ = stores
    r = _upload(client, operator_headers)
    assert r.status_code == 202, r.text
    assert pg.documents[r.json()["document_id"]]["title"] == "manual.pdf"


def test_upload_without_title_or_filename_is_422(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    r = client.post("/v1/admin/teams/acme/documents", content=PDF,
                    headers={**operator_headers, "Content-Type": "application/pdf"})
    assert r.status_code == 422


def test_upload_unsupported_content_type_is_415_and_starts_nothing(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    admin, pg, _ = stores
    r = _upload(client, operator_headers, body=b"hello", ctype="application/zip")
    assert r.status_code == 415
    assert admin.jobs == [] and pg.documents == {} and no_ingest == []


def test_upload_content_type_parameters_are_ignored(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    r = _upload(client, operator_headers, body=b"# Title\n",
                ctype="text/markdown; charset=utf-8", filename="ops.md")
    assert r.status_code == 202, r.text


def test_upload_over_the_cap_is_413_fails_the_job_and_stores_nothing(
    client, stores, acme, operator_headers, doc_root, no_ingest, monkeypatch,
):
    admin, pg, _ = stores
    monkeypatch.setattr("kwim_api.routers.admin_documents._max_bytes", lambda: 100)
    r = _upload(client, operator_headers)
    assert r.status_code == 413
    assert pg.documents == {} and no_ingest == []
    assert all(j["status"] == "failed" for j in admin.jobs)
    assert not (doc_root / "acme" / "originals").exists()


def test_upload_while_an_ingest_is_running_is_409(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    first = _upload(client, operator_headers)
    assert first.status_code == 202, first.text
    second = _upload(client, operator_headers, body=PDF + b"2")
    assert second.status_code == 409
    assert first.json()["job_id"] in second.text


def test_upload_requires_an_operator_session(
    client, stores, acme, team_key_headers, doc_root, no_ingest,
):
    assert _upload(client, {}).status_code == 401
    assert _upload(client, team_key_headers).status_code == 401


def test_upload_to_an_unknown_team_is_404(
    client, stores, operator_headers, doc_root, no_ingest,
):
    r = client.post("/v1/admin/teams/nosuch/documents", content=PDF,
                    params={"filename": "m.pdf"},
                    headers={**operator_headers, "Content-Type": "application/pdf"})
    assert r.status_code == 404


def test_new_revision_increments_and_logs_a_revise_row(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    admin, pg, _ = stores
    doc_id = _upload(client, operator_headers).json()["document_id"]
    admin.jobs[0]["status"] = "succeeded"
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/revisions",
                    content=PDF + b"v2", params={"filename": "manual-v2.pdf", "label": "rev B"},
                    headers={**operator_headers, "Content-Type": "application/pdf"})
    assert r.status_code == 202, r.text
    assert r.json()["revision_number"] == 2
    assert pg.commits[-1]["operation"] == "revise"
    assert pg.commits[-1]["payload"]["label"] == "rev B"


def test_new_revision_of_an_unknown_document_is_404(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    r = client.post(f"/v1/admin/teams/acme/documents/{uuid.uuid4()}/revisions",
                    content=PDF, headers={**operator_headers,
                                          "Content-Type": "application/pdf"})
    assert r.status_code == 404


def test_list_and_detail_return_documents_and_revisions(
    client, stores, acme, operator_headers, doc_root, no_ingest,
):
    up = _upload(client, operator_headers, title="Pump manual").json()
    listing = client.get("/v1/admin/teams/acme/documents", headers=operator_headers)
    assert listing.status_code == 200
    assert [d["document_id"] for d in listing.json()["items"]] == [up["document_id"]]

    detail = client.get(f"/v1/admin/teams/acme/documents/{up['document_id']}",
                        headers=operator_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["title"] == "Pump manual"
    assert body["revisions"][0]["revision_id"] == up["revision_id"]
    assert body["revisions"][0]["status"] == "ingesting"
    assert "extracted" not in body["revisions"][0]


def test_detail_of_an_unknown_document_is_404(
    client, stores, acme, operator_headers, doc_root,
):
    r = client.get(f"/v1/admin/teams/acme/documents/{uuid.uuid4()}",
                   headers=operator_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Ingest job
# ---------------------------------------------------------------------------

async def _stored_revision(pg, root, data=PDF, mime="application/pdf", document_id=None):
    store = DocumentStorage(str(root))
    sha, size = await store.save_original("acme", _chunks(data), max_bytes=10**6)
    if document_id is None:
        document_id = (await pg.create_document(
            "acme", title="t", tags=[], created_by="alice"))["id"]
    rev = await pg.add_revision("acme", document_id=document_id, sha256=sha,
                                mime_type=mime, filename="f", byte_size=size,
                                label=None, uploaded_by="alice")
    return document_id, rev["id"], sha


async def test_ingest_without_an_extractor_fails_the_revision_and_document(
    stores, doc_root, monkeypatch,
):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {})
    doc_id, rev_id, _ = await _stored_revision(pg, doc_root)
    detail = await ingest.ingest_revision(pg, "acme", doc_id, rev_id)
    assert detail["status"] == "failed"
    assert "no extractor for application/pdf" in detail["error"]
    assert pg.revisions[rev_id]["status"] == "failed"
    assert pg.documents[doc_id]["status"] == "failed"


def _fake_extractor(path, figures_dir):
    with open(path, "rb") as fh:
        n = len(fh.read())
    return ingest.Extraction(extracted={"sections": [], "bytes": n},
                             extractor="fake", extractor_version="1", page_count=3)


async def test_ingest_with_an_extractor_makes_the_revision_current(
    stores, doc_root, monkeypatch,
):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"application/pdf": _fake_extractor})
    doc_id, rev_id, _ = await _stored_revision(pg, doc_root)
    detail = await ingest.ingest_revision(pg, "acme", doc_id, rev_id)
    assert detail["status"] == "ready" and detail["page_count"] == 3
    assert pg.revisions[rev_id]["extracted"] == {"sections": [], "bytes": len(PDF)}
    assert pg.documents[doc_id]["current_revision"] == rev_id
    assert pg.documents[doc_id]["status"] == "ready"


async def test_ingest_with_a_missing_original_fails(stores, doc_root, monkeypatch):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"application/pdf": _fake_extractor})
    doc_id, rev_id, sha = await _stored_revision(pg, doc_root)
    os.remove(doc_root / "acme" / "originals" / sha)
    detail = await ingest.ingest_revision(pg, "acme", doc_id, rev_id)
    assert detail["status"] == "failed" and "missing" in detail["error"]


async def test_failed_new_revision_leaves_a_ready_document_ready(
    stores, doc_root, monkeypatch,
):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"application/pdf": _fake_extractor})
    doc_id, rev1, _ = await _stored_revision(pg, doc_root)
    await ingest.ingest_revision(pg, "acme", doc_id, rev1)

    def _boom(path, figures_dir):
        raise RuntimeError("bad file")

    monkeypatch.setattr(ingest, "EXTRACTORS", {"application/pdf": _boom})
    _, rev2, _ = await _stored_revision(pg, doc_root, data=PDF + b"2", document_id=doc_id)
    detail = await ingest.ingest_revision(pg, "acme", doc_id, rev2)
    assert detail["status"] == "failed" and "bad file" in detail["error"]
    assert pg.documents[doc_id]["status"] == "ready"
    assert pg.documents[doc_id]["current_revision"] == rev1


async def test_retention_removes_the_oldest_revisions_and_their_unshared_files(
    stores, doc_root, monkeypatch,
):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"application/pdf": _fake_extractor})
    object.__setattr__(settings, "documents_max_revisions", 2)
    try:
        doc_id, rev1, sha1 = await _stored_revision(pg, doc_root, data=PDF + b"1")
        await ingest.ingest_revision(pg, "acme", doc_id, rev1)
        _, rev2, sha2 = await _stored_revision(pg, doc_root, data=PDF + b"2",
                                               document_id=doc_id)
        await ingest.ingest_revision(pg, "acme", doc_id, rev2)
        _, rev3, sha3 = await _stored_revision(pg, doc_root, data=PDF + b"3",
                                               document_id=doc_id)
        detail = await ingest.ingest_revision(pg, "acme", doc_id, rev3)
    finally:
        object.__setattr__(settings, "documents_max_revisions", 50)

    assert detail["pruned_revisions"] == 1
    assert {r["id"] for r in pg.revisions.values()} == {rev2, rev3}
    originals = set(os.listdir(doc_root / "acme" / "originals"))
    assert originals == {sha2, sha3}


async def test_run_ingest_finishes_the_job(stores, doc_root, monkeypatch):
    admin, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"application/pdf": _fake_extractor})
    monkeypatch.setattr(ingest, "PostgresStore", lambda: _NoConnect(pg))
    monkeypatch.setattr(ingest, "FalkorStore", lambda: _NoConnect(_FakeDocsGraph()))
    monkeypatch.setattr(ingest, "document_embedder", lambda: None)
    doc_id, rev_id, _ = await _stored_revision(pg, doc_root)
    job = await admin.create_job(kind="document_ingest", team="acme")
    await ingest.run_ingest(job["id"], "acme", doc_id, rev_id)
    assert admin.jobs[0]["status"] == "succeeded"
    assert admin.jobs[0]["detail"]["revision_id"] == rev_id


class _FakeDocsGraph:
    def __init__(self):
        self.projected: list[dict] = []

    async def project_document(self, team, **kw):
        self.projected.append({"team": team, **kw})


class _FakeEmbedder:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(texts)
        if self.fail:
            raise RuntimeError("embedder down")
        return [[float(i), 1.0] for i in range(len(texts))]


async def test_ingest_projects_chunks_with_vectors(stores, doc_root, monkeypatch):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"text/markdown": ingest.extract_markdown})
    md = b"# Pump\n\n## Removal\n\n1. Undo the bolts.\n2. Lift it out.\n\n![Pump](p.png)\n"
    doc_id, rev_id, _ = await _stored_revision(pg, doc_root, data=md, mime="text/markdown")
    graph, emb = _FakeDocsGraph(), _FakeEmbedder()
    detail = await ingest.ingest_revision(pg, "acme", doc_id, rev_id, falkor=graph, embedder=emb)
    assert detail["status"] == "ready" and detail["chunks"] >= 1
    assert detail["embedded"] == detail["chunks"] and detail["warnings"] == []
    p = graph.projected[0]
    assert p["document"]["id"] == doc_id and p["document"]["revision_id"] == rev_id
    chunk = p["chunks"][0]
    assert chunk["id"].startswith(f"{doc_id}:") and chunk["embedding"] == [0.0, 1.0]
    assert "1. Undo the bolts." in chunk["text"]
    assert emb.calls[0][0].startswith("t > Pump > Removal:")
    section_ids = {s["id"] for s in p["sections"]}
    assert chunk["section_id"] in section_ids
    removal = next(s for s in p["sections"] if s["heading"] == "Removal")
    assert removal["parent"] in section_ids and removal["path"] == "Pump > Removal"
    assert p["figures"][0]["sections"] == [removal["id"]]
    assert chunk["figure_ids"] == [p["figures"][0]["id"]]


async def test_ingest_without_vectors_when_the_embedder_fails(stores, doc_root, monkeypatch):
    _, pg, _ = stores
    monkeypatch.setattr(ingest, "EXTRACTORS", {"text/markdown": ingest.extract_markdown})
    doc_id, rev_id, _ = await _stored_revision(pg, doc_root, data=b"# A\n\ntext\n",
                                               mime="text/markdown")
    graph = _FakeDocsGraph()
    detail = await ingest.ingest_revision(pg, "acme", doc_id, rev_id, falkor=graph,
                                          embedder=_FakeEmbedder(fail=True))
    assert detail["status"] == "ready" and detail["embedded"] == 0
    assert any("embedding failed" in w for w in detail["warnings"])
    assert all(c["embedding"] is None for c in graph.projected[0]["chunks"])


class _NoConnect:
    """Wraps a fake store so run_ingest's connect/close are no-ops."""

    def __init__(self, inner):
        self._inner = inner

    async def connect(self):
        pass

    async def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

async def test_rebuild_replay_skips_document_rows():
    from tests.test_rebuild import _commit_row, _replay

    ff = await _replay([
        _commit_row(seq=1, object_type="document", object_id="d1", operation="commit"),
        _commit_row(seq=2, object_type="document", object_id="d1", operation="retract"),
    ])
    assert ff.calls == []


# ---------------------------------------------------------------------------
# Lifecycle: retract, forget, promote
# ---------------------------------------------------------------------------

class LifecyclePg(FakePgStore):
    def __init__(self):
        super().__init__()
        self.status_set: list[tuple] = []
        self.copies: list[tuple] = []
        self.preflight_ok = True

    async def set_document_status(self, team, document_id, status):
        self.documents[document_id]["status"] = status

    async def delete_preflight(self, team):
        return {"commit_log": self.preflight_ok, "episodic": True, "verifications": True}

    async def delete_commit_log(self, team, object_id):
        before = len(self.commits)
        self.commits = [c for c in self.commits if c["object_id"] != object_id]
        return before - len(self.commits)

    async def document_files(self, team, document_id):
        revs = [r for r in self.revisions.values() if r["document_id"] == document_id]
        return {"revisions": len(revs), "bytes": sum(r["byte_size"] for r in revs),
                "shas": sorted({r["sha256"] for r in revs}), "figures": 0}

    async def delete_document(self, team, document_id):
        self.revisions = {k: r for k, r in self.revisions.items()
                          if r["document_id"] != document_id}
        return self.documents.pop(document_id, None) is not None

    async def copy_document(self, src_team, document_id, dest_team, *, created_by):
        src = self.documents[document_id]
        new = await self.create_document(dest_team, title=src["title"], tags=src["tags"],
                                         created_by=created_by)
        rev = self.revisions[src["current_revision"]]
        copy = await self.add_revision(dest_team, document_id=new["id"], sha256=rev["sha256"],
                                       mime_type=rev["mime_type"], filename=rev["filename"],
                                       byte_size=rev["byte_size"], label=None,
                                       uploaded_by=created_by)
        src["promoted_to"] = new["id"]
        self.copies.append((src_team, document_id, dest_team))
        return {"document_id": new["id"], "revision_id": copy["id"], "sha256": rev["sha256"]}


class LifecycleFalkor:
    def __init__(self):
        self.removed: list[tuple] = []
        self.promoted: list[tuple] = []
        self.previews: dict[str, dict] = {}

    async def remove_document_projection(self, team, document_id):
        self.removed.append((team, document_id))

    async def set_document_promoted(self, team, document_id, universe_id):
        self.promoted.append((team, document_id, universe_id))

    async def forget_preview_set(self, token, doc, ttl):
        self.previews[token] = doc

    async def forget_preview_getdel(self, token):
        return self.previews.pop(token, None)

    async def project_document(self, team, **kw):
        pass


@pytest.fixture
def life(monkeypatch, doc_root):
    from kwim_api.documents import lifecycle
    from kwim_api.runtime import State
    from tests.test_admin_write import FakeAdminStore

    admin, pg, falkor = FakeAdminStore(), LifecyclePg(), LifecycleFalkor()
    monkeypatch.setattr(State, "admin", admin, raising=False)
    monkeypatch.setattr(State, "pg", pg, raising=False)
    monkeypatch.setattr(State, "falkor", falkor, raising=False)
    monkeypatch.setattr(State, "docs_embedder", None, raising=False)
    projected: list[tuple] = []

    async def _record_projection(*args):
        projected.append(args)

    monkeypatch.setattr(lifecycle, "_project_copy", _record_projection)
    pg.seed_schema("acme")
    token = admin.seed_session(operator_id=admin.seed_operator(username="alice"))
    return {"admin": admin, "pg": pg, "falkor": falkor, "projected": projected,
            "headers": {"Authorization": f"Bearer {token}"}}


async def _ready_doc(life, doc_root):
    pg = life["pg"]
    doc_id, rev_id, sha = await _stored_revision(pg, doc_root)
    await pg.finish_revision("acme", rev_id, status="ready", extracted={"sections": []},
                             extractor="x", extractor_version="1")
    return doc_id, rev_id, sha


async def test_retract_removes_the_projection_and_logs_it(client, life, doc_root):
    doc_id, _, _ = await _ready_doc(life, doc_root)
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/retract",
                    json={"reason": "superseded"}, headers=life["headers"])
    assert r.status_code == 200, r.text
    assert life["pg"].documents[doc_id]["status"] == "retracted"
    assert life["falkor"].removed == [("acme", doc_id)]
    commit = life["pg"].commits[-1]
    assert commit["operation"] == "retract" and commit["provenance"]["reason"] == "superseded"
    again = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/retract",
                        json={}, headers=life["headers"])
    assert again.status_code == 409


async def test_forget_needs_a_matching_single_use_token(client, life, doc_root):
    doc_id, _, sha = await _ready_doc(life, doc_root)
    life["pg"].commits.append({"team": "acme", "object_id": doc_id, "operation": "commit"})
    preview = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/forget/preview",
                          headers=life["headers"])
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["revisions"] == 1 and body["bytes"] == len(PDF)
    bad = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/forget",
                      json={"preview_token": "nope"}, headers=life["headers"])
    assert bad.status_code == 409
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/forget",
                    json={"preview_token": body["preview_token"]}, headers=life["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["files_removed"] == 1 and r.json()["commit_log_rows"] == 1
    assert doc_id not in life["pg"].documents
    assert not (doc_root / "acme" / "originals" / sha).exists()
    assert life["falkor"].removed == [("acme", doc_id)]
    reused = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/forget",
                         json={"preview_token": body["preview_token"]},
                         headers=life["headers"])
    assert reused.status_code in (404, 409)


async def test_forget_keeps_a_file_another_document_still_uses(client, life, doc_root):
    doc_a, _, sha = await _ready_doc(life, doc_root)
    await _stored_revision(life["pg"], doc_root)            # same bytes, second document
    token = client.post(f"/v1/admin/teams/acme/documents/{doc_a}/forget/preview",
                        headers=life["headers"]).json()["preview_token"]
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_a}/forget",
                    json={"preview_token": token}, headers=life["headers"])
    assert r.json()["files_removed"] == 0
    assert (doc_root / "acme" / "originals" / sha).is_file()


async def test_forget_is_refused_when_the_role_cannot_delete(client, life, doc_root):
    doc_id, _, _ = await _ready_doc(life, doc_root)
    life["pg"].preflight_ok = False
    token = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/forget/preview",
                        headers=life["headers"]).json()["preview_token"]
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/forget",
                    json={"preview_token": token}, headers=life["headers"])
    assert r.status_code == 503
    assert doc_id in life["pg"].documents


async def test_admin_promote_copies_to_the_universe(client, life, doc_root):
    doc_id, _, sha = await _ready_doc(life, doc_root)
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/promote",
                    headers=life["headers"])
    assert r.status_code == 200, r.text
    uid = r.json()["universe_document_id"]
    assert life["pg"].copies == [("acme", doc_id, "universe")]
    assert (doc_root / "universe" / "originals" / sha).is_file()
    commit = life["pg"].commits[-1]
    assert commit["team"] == "universe" and commit["source_kind"] == "promotion"
    assert commit["provenance"]["promoted_from_id"] == doc_id
    assert life["falkor"].promoted == [("acme", doc_id, uid)]
    assert len(life["projected"]) == 1
    again = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/promote",
                        headers=life["headers"])
    assert again.status_code == 409


async def test_promote_refuses_a_document_that_is_not_ready(client, life, doc_root):
    doc_id, _, _ = await _stored_revision(life["pg"], doc_root)
    r = client.post(f"/v1/admin/teams/acme/documents/{doc_id}/promote",
                    headers=life["headers"])
    assert r.status_code == 409


async def test_agent_promote_needs_the_promote_capability(client, life, doc_root):
    doc_id, _, _ = await _ready_doc(life, doc_root)
    denied = client.post(f"/v1/documents/{doc_id}/promote",
                         headers={"Authorization": "Bearer otherkey"})
    assert denied.status_code == 403
    r = client.post(f"/v1/documents/{doc_id}/promote",
                    headers={"Authorization": "Bearer promoter"})
    assert r.status_code == 200, r.text
    assert life["pg"].commits[-1]["proposed_by"] == "promot"
