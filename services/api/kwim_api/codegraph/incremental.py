"""File discovery + content-hash incremental support.

Walks a checked-out repo for source files, hashes each with xxh3, and reads the
current git commit.
"""
from __future__ import annotations

import os
import subprocess

import pathspec
import xxhash

from ..config import settings

# Discovery scope comes from codegraph.discovery.* in kwim.defaults.yaml. See
# docs/DESIGN.md, "The code graph".
LANG_BY_EXT = settings.cg_lang_by_ext
_SKIP_DIRS = settings.cg_skip_dirs
_IGNORE_FILES = settings.cg_ignore_files


def _is_test_file(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _load_ignore_spec(repo_dir: str) -> pathspec.PathSpec | None:
    patterns: list[str] = []
    for fname in _IGNORE_FILES:
        path = os.path.join(repo_dir, fname)
        try:
            with open(path) as fh:
                patterns.extend(fh.read().splitlines())
        except OSError:
            continue
    if not patterns:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def content_hash(data: bytes) -> str:
    return xxhash.xxh3_64_hexdigest(data)


def git_head(repo_dir: str) -> str:
    """Resolve the checked-out commit SHA, with the git binary or by reading .git."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return _git_head_from_files(repo_dir)


def _git_head_from_files(repo_dir: str) -> str:
    git_dir = os.path.join(repo_dir, ".git")
    try:
        head = open(os.path.join(git_dir, "HEAD")).read().strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head                                  # detached HEAD: HEAD holds the SHA
    ref = head.split(" ", 1)[1].strip()              # e.g. refs/heads/main
    # loose ref first
    try:
        return open(os.path.join(git_dir, ref)).read().strip()
    except OSError:
        pass
    # packed-refs fallback
    try:
        for line in open(os.path.join(git_dir, "packed-refs")):
            line = line.strip()
            if line and not line.startswith(("#", "^")) and line.endswith(ref):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return ""


def discover(repo_dir: str) -> list[tuple[str, str]]:
    """Return (rel_path, lang) for every indexable source file under repo_dir.

    Skips _SKIP_DIRS, test files, and paths matched by .gitignore or .cgignore.
    """
    spec = _load_ignore_spec(repo_dir)
    out: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(repo_dir):
        rel_root = os.path.relpath(root, repo_dir)
        # Skip ignored directories (matched with a trailing slash, as gitignore expects).
        kept = []
        for d in dirs:
            if d in _SKIP_DIRS:
                continue
            rel_d = d if rel_root == "." else f"{rel_root}/{d}"
            if spec is not None and spec.match_file(rel_d + "/"):
                continue
            kept.append(d)
        dirs[:] = kept
        for fn in files:
            ext = os.path.splitext(fn)[1]
            lang = LANG_BY_EXT.get(ext)
            if not lang:
                continue
            if _is_test_file(fn):                    # skip test modules anywhere in the tree
                continue
            abs_path = os.path.join(root, fn)
            if os.path.islink(abs_path):
                continue
            rel = os.path.relpath(abs_path, repo_dir)
            if spec is not None and spec.match_file(rel):
                continue
            out.append((rel, lang))
    return out
