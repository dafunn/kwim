"""Import documents and skills into a team through the admin API, as an operator.

    python -m kwim_api.admin_import --team acme documents manual.pdf notes.md \\
            [--title TITLE] [--tags a,b] [--label LABEL] [--confirm-new-revision]
    python -m kwim_api.admin_import --team acme skills skills/ check-disk/SKILL.md \\
            [--owner NAME] [--confirm-new-revision]

Common options: --url (default KWIM_BASE_URL), --ca-file or --insecure for the
service's certificate, and --job-timeout for each document's ingest.

Credentials come from KWIM_ADMIN_USERNAME and KWIM_ADMIN_PASSWORD. Without the
password the tool asks for it without echoing it.

A document is matched to an existing one by title (by default its file name), a
skill by the name in its frontmatter. When it exists, the tool says so and asks
before uploading a new revision; --confirm-new-revision answers yes for every
item. Without a terminal and without the flag, an existing item is an error.

A skill path is a SKILL.md, a directory holding one, or a directory whose
subdirectories hold one each. A skill is a single file: only each SKILL.md is
read, and other files in its directory are ignored. Each skill is checked
locally before upload.

Each document upload waits for its ingest job, because a team runs one ingest
at a time. Exit status is 0 when every item was imported or declined, else 1.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .skills.format import SkillFormatError, parse_skill

MIME = {".pdf": "application/pdf", ".md": "text/markdown", ".markdown": "text/markdown"}

_PAGE = 500
_POLL_S = 2.0


class ItemError(Exception):
    """One item could not be imported; the message says why."""


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    return f"{resp.status_code} {detail if detail is not None else resp.text[:200]}"


def _check(resp: httpx.Response, *ok: int) -> dict[str, Any]:
    if resp.status_code not in ok:
        raise ItemError(_detail(resp))
    return resp.json() if resp.content else {}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 16):
            h.update(block)
    return h.hexdigest()


class Confirm:
    """Asks before a new revision; `always` answers yes without asking."""

    def __init__(self, always: bool, ask: Callable[[str], str] = input,
                 interactive: Callable[[], bool] = sys.stdin.isatty):
        self.always = always
        self._ask = ask
        self._interactive = interactive

    def __call__(self, message: str) -> bool:
        print(message)
        if self.always:
            print("  confirmed by --confirm-new-revision")
            return True
        if not self._interactive():
            raise ItemError("it already exists and there is no terminal to confirm a new "
                               "revision; pass --confirm-new-revision")
        return self._ask("  upload a new revision? [y/N] ").strip().lower() in ("y", "yes")


# --- Documents -------------------------------------------------------------------

def _documents_by_title(http: httpx.Client, team: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    offset = 0
    while True:
        page = _check(http.get(f"/v1/admin/teams/{team}/documents",
                               params={"limit": _PAGE, "offset": offset}), 200)["items"]
        for d in page:
            out.setdefault(d["title"], []).append(d)
        if len(page) < _PAGE:
            return out
        offset += _PAGE


def _wait_job(http: httpx.Client, job_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        job = _check(http.get(f"/v1/admin/jobs/{job_id}"), 200)
        if job["status"] != "running":
            return job
        if time.monotonic() > deadline:
            raise ItemError(f"ingest job {job_id} still running after {timeout_s:.0f}s; "
                               "it continues in the service")
        time.sleep(_POLL_S)


def import_document(http: httpx.Client, team: str, path: Path, *, title: str | None,
                    tags: str | None, label: str | None, existing: dict[str, list],
                    confirm: Confirm, job_timeout_s: float) -> str:
    mime = MIME.get(path.suffix.lower())
    if mime is None:
        raise ItemError(f"unsupported file type {path.suffix!r}; "
                           f"use {', '.join(sorted(MIME))}")
    title = title or path.name
    params: dict[str, str] = {"filename": path.name}
    if label:
        params["label"] = label
    matches = existing.get(title, [])
    if len(matches) > 1:
        raise ItemError(f"{len(matches)} documents are titled {title!r}; "
                           "revise the right one in the console")
    if matches:
        doc = matches[0]
        if doc["status"] == "retracted":
            raise ItemError(f"document {title!r} is retracted")
        detail = _check(http.get(f"/v1/admin/teams/{team}/documents/{doc['document_id']}"), 200)
        current = next((r for r in detail["revisions"]
                        if r["revision_id"] == detail["current_revision"]), None)
        note = ""
        if current is not None:
            note = f", current revision {current['revision_number']}"
            if current["sha256"] == _sha256(path):
                note += "; this file is identical to it"
        if not confirm(f"document {title!r} already exists in {team} ({doc['status']}{note})"):
            return "declined"
        url = f"/v1/admin/teams/{team}/documents/{doc['document_id']}/revisions"
        verb = "revised"
    else:
        params["title"] = title
        if tags:
            params["tags"] = tags
        url = f"/v1/admin/teams/{team}/documents"
        verb = "created"
    with path.open("rb") as fh:
        upload = _check(http.post(url, params=params, content=fh,
                                  headers={"Content-Type": mime}), 202)
    if not matches:
        existing[title] = [{"document_id": upload["document_id"], "title": title,
                            "status": "ingesting"}]
    job = _wait_job(http, upload["job_id"], job_timeout_s)
    if job["status"] != "succeeded":
        raise ItemError(f"ingest failed: {(job.get('detail') or {}).get('error')}")
    chunks = (job.get("detail") or {}).get("chunks")
    return f"{verb} at revision {upload['revision_number']}, ready ({chunks} chunks)"


# --- Skills ----------------------------------------------------------------------

def skill_files(paths: list[Path]) -> list[Path]:
    """Each SKILL.md the paths name, in order."""
    out: list[Path] = []
    for p in paths:
        if p.is_file():
            out.append(p)
        elif (p / "SKILL.md").is_file():
            out.append(p / "SKILL.md")
        elif p.is_dir():
            found = sorted(p.glob("*/SKILL.md"))
            if not found:
                raise ItemError(f"{p}: no SKILL.md in it or in its subdirectories")
            out.extend(found)
        else:
            raise ItemError(f"{p}: no such file or directory")
    return out


def _skill_names(http: httpx.Client, team: str) -> set[str]:
    return {s["name"] for s in _check(http.get(f"/v1/admin/teams/{team}/skills"), 200)["items"]}


def import_skill(http: httpx.Client, team: str, path: Path, *, owner: str | None,
                 existing: set[str], confirm: Confirm) -> str:
    text = path.read_text()
    try:
        name = parse_skill(text).name
    except SkillFormatError as exc:
        raise ItemError(f"SKILL.md {exc}") from None
    if name not in existing:
        body: dict[str, Any] = {"text": text}
        if owner:
            body["owner"] = owner
        created = _check(http.post(f"/v1/admin/teams/{team}/skills", json=body), 201)
        existing.add(name)
        return f"created {name} version {created['version']}"
    skill = _check(http.get(f"/v1/admin/teams/{team}/skills/{name}"), 200)
    note = f"version {skill['version']}"
    if skill["status"] == "retired":
        note += ", retired; a new version stays retired until restored"
    if skill["text"] == text:
        note += "; this text is identical to it"
    if not confirm(f"skill {name!r} already exists in {team} ({note})"):
        return "declined"
    revised = _check(http.post(f"/v1/admin/teams/{team}/skills/{name}/revisions",
                               json={"text": text}), 200)
    return f"revised {name} to version {revised['version']}"


# --- Session and main ------------------------------------------------------------

def _login(http: httpx.Client) -> None:
    username = os.environ.get("KWIM_ADMIN_USERNAME") or input("operator username: ").strip()
    password = os.environ.get("KWIM_ADMIN_PASSWORD") or getpass.getpass("operator password: ")
    session = _check(http.post("/v1/admin/session",
                               json={"username": username, "password": password}), 200)
    http.headers["Authorization"] = f"Bearer {session['token']}"


def run(http: httpx.Client, args: argparse.Namespace, confirm: Confirm) -> int:
    failed = 0
    if args.kind == "documents":
        if args.title and len(args.paths) > 1:
            print("ERROR: --title applies to a single file", file=sys.stderr)
            return 1
        try:
            existing = _documents_by_title(http, args.team)
        except ItemError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        for path in args.paths:
            try:
                result = import_document(http, args.team, path, title=args.title,
                                         tags=args.tags, label=args.label, existing=existing,
                                         confirm=confirm, job_timeout_s=args.job_timeout)
                print(f"{path}: {result}")
            except ItemError as exc:
                failed += 1
                print(f"{path}: FAILED {exc}", file=sys.stderr)
    else:
        try:
            files = skill_files(args.paths)
            names = _skill_names(http, args.team)
        except ItemError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        for path in files:
            try:
                result = import_skill(http, args.team, path, owner=args.owner,
                                      existing=names, confirm=confirm)
                print(f"{path}: {result}")
            except ItemError as exc:
                failed += 1
                print(f"{path}: FAILED {exc}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("KWIM_BASE_URL"),
                        help="the KWIM service (default KWIM_BASE_URL)")
    parser.add_argument("--team", required=True)
    parser.add_argument("--confirm-new-revision", action="store_true",
                        help="upload a new revision of an existing item without asking")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--ca-file", help="CA bundle for the service's certificate")
    tls.add_argument("--insecure", action="store_true",
                     help="do not verify the service's certificate")
    sub = parser.add_subparsers(dest="kind", required=True)
    docs = sub.add_parser("documents", help="upload PDF or Markdown files")
    docs.add_argument("paths", nargs="+", type=Path)
    docs.add_argument("--title", help="the document title (one file only; default the file name)")
    docs.add_argument("--tags", help="comma-separated tags for a new document")
    docs.add_argument("--label", help="a revision label, such as 'rev B'")
    docs.add_argument("--job-timeout", type=float, default=1800.0,
                      help="seconds to wait for each ingest (default 1800)")
    skills = sub.add_parser("skills", help="add or revise skills from SKILL.md files "
                                            "(other files in a skill directory are ignored)")
    skills.add_argument("paths", nargs="+", type=Path)
    skills.add_argument("--owner", help="owner of new skills (default the operator)")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or KWIM_BASE_URL is required")

    verify: bool | str = args.ca_file or not args.insecure
    with httpx.Client(base_url=args.url.rstrip("/"), verify=verify,
                      timeout=httpx.Timeout(60.0, write=600.0)) as http:
        try:
            _login(http)
        except (ItemError, httpx.HTTPError) as exc:
            print(f"ERROR: login failed: {exc}", file=sys.stderr)
            return 1
        try:
            return run(http, args, Confirm(args.confirm_new_revision))
        except httpx.HTTPError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            try:
                http.delete("/v1/admin/session")
            except httpx.HTTPError:
                pass


if __name__ == "__main__":
    sys.exit(main())
