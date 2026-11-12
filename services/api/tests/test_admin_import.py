"""kwim_api.admin_import against a fake admin API: new and existing documents and
skills, the confirmation prompt and --confirm-new-revision, ingest waits, and
skill path discovery."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from kwim_api import admin_import
from kwim_api.admin_import import Confirm, ItemError


def _skill_md(name: str, body: str = "Run the report.") -> str:
    return f"---\nname: {name}\ndescription: Does {name}.\n---\n\n{body}\n"


class FakeAdmin:
    """Just enough of /v1/admin for the import tool."""

    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.skills: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.job_status = "succeeded"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/admin/session":
            if method == "POST":
                return httpx.Response(200, json={"token": "t"})
            return httpx.Response(204)
        assert request.headers["Authorization"] == "Bearer t"
        parts = path.split("/")[3:]  # after /v1/admin
        if parts[0] == "jobs":
            return httpx.Response(200, json=self.jobs[parts[1]])
        team, kind, rest = parts[1], parts[2], parts[3:]
        if team != "acme":
            return httpx.Response(404, json={"detail": f"team {team!r} not found"})
        if kind == "documents":
            return self._documents(request, method, rest)
        return self._skills(request, method, rest)

    def _documents(self, request, method, rest):
        if method == "GET" and not rest:
            return httpx.Response(200, json={"items": [
                {k: d[k] for k in ("document_id", "title", "status")}
                for d in self.documents.values()]})
        if method == "GET":
            d = self.documents[rest[0]]
            return httpx.Response(200, json={**d, "current_revision": d["revisions"][-1]["revision_id"]})
        body = request.read()
        sha = hashlib.sha256(body).hexdigest()
        if rest:
            doc = self.documents[rest[0]]
        else:
            doc_id = f"d{len(self.documents) + 1}"
            doc = self.documents[doc_id] = {"document_id": doc_id, "status": "ready",
                                            "title": request.url.params["title"], "revisions": []}
        n = len(doc["revisions"]) + 1
        doc["revisions"].append({"revision_id": f"{doc['document_id']}r{n}",
                                 "revision_number": n, "sha256": sha})
        job_id = f"j{len(self.jobs) + 1}"
        self.jobs[job_id] = {"job_id": job_id, "status": self.job_status,
                             "detail": {"chunks": 7} if self.job_status == "succeeded"
                             else {"error": "no text layer"}}
        self.uploads.append({"params": dict(request.url.params),
                             "type": request.headers["Content-Type"], "revision": n})
        return httpx.Response(202, json={"document_id": doc["document_id"], "revision_number": n,
                                         "job_id": job_id})

    def _skills(self, request, method, rest):
        if method == "GET" and not rest:
            return httpx.Response(200, json={"items": [
                {"name": n, "version": s["version"]} for n, s in self.skills.items()]})
        if method == "GET":
            s = self.skills[rest[0]]
            return httpx.Response(200, json={"name": rest[0], **s})
        body = json.loads(request.read())
        name = body["text"].split("name: ")[1].split("\n")[0]
        if rest:
            s = self.skills[rest[0]]
            s.update(version=s["version"] + 1, text=body["text"])
            return httpx.Response(200, json={"name": rest[0], "version": s["version"]})
        self.skills[name] = {"version": 1, "status": "current", "text": body["text"],
                             "owner": body.get("owner")}
        return httpx.Response(201, json={"name": name, "version": 1})


@pytest.fixture
def fake():
    return FakeAdmin()


@pytest.fixture
def http(fake):
    with httpx.Client(base_url="http://kwim", transport=httpx.MockTransport(fake.handler),
                      headers={"Authorization": "Bearer t"}) as client:
        yield client


def _args(kind, paths, **kw):
    ns = {"kind": kind, "paths": [Path(p) for p in paths], "team": "acme", "title": None,
          "tags": None, "label": None, "job_timeout": 5.0, "owner": None}
    ns.update(kw)
    return type("Args", (), ns)


def _confirm(answer: str | None = None, *, always=False, interactive=True):
    asked: list[str] = []

    def ask(prompt):
        asked.append(prompt)
        return answer or ""
    c = Confirm(always, ask=ask, interactive=lambda: interactive)
    c.asked = asked
    return c


# --- Documents -------------------------------------------------------------------

def test_new_documents_upload_with_type_title_and_tags(fake, http, tmp_path):
    (tmp_path / "pump.pdf").write_bytes(b"%PDF-1")
    (tmp_path / "notes.md").write_text("# Notes\n")
    rc = admin_import.run(http, _args("documents", [tmp_path / "pump.pdf", tmp_path / "notes.md"],
                                      tags="pumps"), _confirm())
    assert rc == 0
    assert [u["type"] for u in fake.uploads] == ["application/pdf", "text/markdown"]
    assert fake.uploads[0]["params"] == {"filename": "pump.pdf", "title": "pump.pdf",
                                         "tags": "pumps"}


def test_existing_document_asks_and_says_when_the_file_is_identical(fake, http, tmp_path, capsys):
    f = tmp_path / "pump.pdf"
    f.write_bytes(b"%PDF-1")
    admin_import.run(http, _args("documents", [f]), _confirm())
    c = _confirm("y")
    assert admin_import.run(http, _args("documents", [f], label="rev B"), c) == 0
    assert c.asked and "identical" in capsys.readouterr().out
    assert fake.uploads[-1] == {"params": {"filename": "pump.pdf", "label": "rev B"},
                                "type": "application/pdf", "revision": 2}


def test_declining_a_new_revision_uploads_nothing(fake, http, tmp_path, capsys):
    f = tmp_path / "pump.pdf"
    f.write_bytes(b"%PDF-1")
    admin_import.run(http, _args("documents", [f]), _confirm())
    assert admin_import.run(http, _args("documents", [f]), _confirm("n")) == 0
    assert len(fake.uploads) == 1
    assert "declined" in capsys.readouterr().out


def test_confirm_flag_revises_without_asking(fake, http, tmp_path):
    f = tmp_path / "pump.pdf"
    f.write_bytes(b"%PDF-1")
    admin_import.run(http, _args("documents", [f]), _confirm())
    f.write_bytes(b"%PDF-2")
    c = _confirm(always=True, interactive=False)
    assert admin_import.run(http, _args("documents", [f]), c) == 0
    assert not c.asked and fake.uploads[-1]["revision"] == 2


def test_existing_item_without_a_terminal_or_the_flag_fails(fake, http, tmp_path, capsys):
    f = tmp_path / "pump.pdf"
    f.write_bytes(b"%PDF-1")
    admin_import.run(http, _args("documents", [f]), _confirm())
    assert admin_import.run(http, _args("documents", [f]), _confirm(interactive=False)) == 1
    assert "--confirm-new-revision" in capsys.readouterr().err
    assert len(fake.uploads) == 1


def test_failed_ingest_and_unsupported_type_fail_the_run(fake, http, tmp_path, capsys):
    fake.job_status = "failed"
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1")
    (tmp_path / "pump.docx").write_bytes(b"x")
    rc = admin_import.run(http, _args("documents", [tmp_path / "scan.pdf", tmp_path / "pump.docx"]),
                          _confirm())
    err = capsys.readouterr().err
    assert rc == 1 and "no text layer" in err and "unsupported file type" in err


def test_title_with_several_files_is_refused(http, tmp_path):
    assert admin_import.run(http, _args("documents", ["a.pdf", "b.pdf"], title="T"),
                            _confirm()) == 1


def test_unknown_team_is_reported(http, tmp_path, capsys):
    (tmp_path / "a.pdf").write_bytes(b"x")
    assert admin_import.run(http, _args("documents", [tmp_path / "a.pdf"], team="nope"),
                            _confirm()) == 1
    assert "not found" in capsys.readouterr().err


# --- Skills ----------------------------------------------------------------------

def _write_skill(root: Path, name: str, body: str = "Run the report.") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_md(name, body))
    return d


def test_skill_files_accepts_a_file_a_skill_directory_and_a_parent(tmp_path):
    a = _write_skill(tmp_path / "set", "check-a")
    b = _write_skill(tmp_path / "set", "check-b")
    c = _write_skill(tmp_path, "check-c")
    found = admin_import.skill_files([tmp_path / "set", c, a / "SKILL.md"])
    assert found == [a / "SKILL.md", b / "SKILL.md", c / "SKILL.md", a / "SKILL.md"]
    with pytest.raises(ItemError):
        admin_import.skill_files([tmp_path / "missing"])


def test_new_skills_are_created_with_the_owner(fake, http, tmp_path):
    _write_skill(tmp_path, "check-a")
    _write_skill(tmp_path, "check-b")
    assert admin_import.run(http, _args("skills", [tmp_path], owner="alice"), _confirm()) == 0
    assert {n: s["owner"] for n, s in fake.skills.items()} == {"check-a": "alice",
                                                              "check-b": "alice"}


def test_existing_skill_asks_then_revises(fake, http, tmp_path, capsys):
    d = _write_skill(tmp_path, "check-a")
    admin_import.run(http, _args("skills", [d]), _confirm())
    _write_skill(tmp_path, "check-a", "Run the new report.")
    c = _confirm("yes")
    assert admin_import.run(http, _args("skills", [d]), c) == 0
    assert c.asked and fake.skills["check-a"]["version"] == 2
    assert "already exists in acme (version 1)" in capsys.readouterr().out


def test_identical_skill_text_is_noted(fake, http, tmp_path, capsys):
    d = _write_skill(tmp_path, "check-a")
    admin_import.run(http, _args("skills", [d]), _confirm())
    admin_import.run(http, _args("skills", [d]), _confirm("n"))
    assert "identical" in capsys.readouterr().out
    assert fake.skills["check-a"]["version"] == 1


def test_a_malformed_or_credential_bearing_skill_is_not_sent(fake, http, tmp_path, capsys):
    bad = _write_skill(tmp_path, "check-a", "password: hunter2hunter2")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("no frontmatter")
    rc = admin_import.run(http, _args("skills", [bad, tmp_path / "broken"]), _confirm())
    err = capsys.readouterr().err
    assert rc == 1 and "credential" in err and "hunter2" not in err
    assert fake.skills == {}
