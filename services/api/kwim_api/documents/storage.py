"""Content-addressed file storage on the documents volume.

    <root>/<team>/originals/<sha256>             uploaded files
    <root>/<team>/figures/<sha256>/<figure>.png  figures cut from an original
    <root>/<team>/tmp/                           uploads and extractions in progress

A file is written under tmp/ and renamed into place once complete, so a reader
never sees a partial file. Files are never modified after the rename.
"""
import asyncio
import hashlib
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator

from ..stores.postgres import _IDENT

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIGURE_ID = re.compile(r"^f[0-9]{1,6}$")
_OBJECT_KEY = re.compile(r"^figures/[0-9a-f]{64}/f[0-9]{1,6}\.png$")


class UploadTooLarge(Exception):
    """The upload exceeded the size limit; nothing was stored."""


class DocumentStorage:
    def __init__(self, root: str) -> None:
        self._root = root

    def _team_dir(self, team: str) -> str:
        if not _IDENT.match(team):
            raise ValueError(f"unsafe team identifier: {team!r}")
        return os.path.join(self._root, team)

    def original_path(self, team: str, sha256: str) -> str:
        if not _SHA256.match(sha256):
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        return os.path.join(self._team_dir(team), "originals", sha256)

    def has_original(self, team: str, sha256: str) -> bool:
        return os.path.isfile(self.original_path(team, sha256))

    async def save_original(self, team: str, chunks: AsyncIterator[bytes],
                            max_bytes: int) -> tuple[str, int]:
        """Stream `chunks` to the team's originals, returning (sha256, size).
        Raises UploadTooLarge past `max_bytes`, leaving nothing behind. An upload
        whose content is already stored is discarded and the existing file kept."""
        tmp_dir = os.path.join(self._team_dir(team), "tmp")
        await asyncio.to_thread(os.makedirs, tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.part")
        digest = hashlib.sha256()
        size = 0
        fh = await asyncio.to_thread(open, tmp_path, "wb")
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(f"upload exceeds {max_bytes} bytes")
                digest.update(chunk)
                await asyncio.to_thread(fh.write, chunk)
            await asyncio.to_thread(fh.flush)
            await asyncio.to_thread(os.fsync, fh.fileno())
        except BaseException:
            await asyncio.to_thread(fh.close)
            await asyncio.to_thread(_unlink_quietly, tmp_path)
            raise
        await asyncio.to_thread(fh.close)

        sha256 = digest.hexdigest()
        final_path = self.original_path(team, sha256)
        await asyncio.to_thread(os.makedirs, os.path.dirname(final_path), exist_ok=True)
        if await asyncio.to_thread(os.path.isfile, final_path):
            await asyncio.to_thread(_unlink_quietly, tmp_path)
        else:
            await asyncio.to_thread(os.replace, tmp_path, final_path)
        return sha256, size

    def scratch_dir(self, team: str) -> str:
        """A new empty directory under the team's tmp/ for an extraction."""
        path = os.path.join(self._team_dir(team), "tmp", uuid.uuid4().hex)
        os.makedirs(path)
        return path

    def figure_key(self, sha256: str, figure_id: str) -> str:
        if not _SHA256.match(sha256) or not _FIGURE_ID.match(figure_id):
            raise ValueError(f"bad figure key: {sha256!r}/{figure_id!r}")
        return f"figures/{sha256}/{figure_id}.png"

    def path_for_key(self, team: str, key: str) -> str:
        """Absolute path of a stored object key such as figures/<sha>/<id>.png."""
        if not _OBJECT_KEY.match(key):
            raise ValueError(f"bad object key: {key!r}")
        return os.path.join(self._team_dir(team), key)

    def store_figure(self, team: str, sha256: str, figure_id: str, src: str) -> str:
        """Move a rendered figure into place. Returns its object key."""
        key = self.figure_key(sha256, figure_id)
        dest = self.path_for_key(team, key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(src, dest)
        return key

    def delete_figures(self, team: str, sha256: str) -> None:
        """Remove every figure cut from one original."""
        if not _SHA256.match(sha256):
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        shutil.rmtree(os.path.join(self._team_dir(team), "figures", sha256),
                      ignore_errors=True)

    def copy_to_team(self, src_team: str, dest_team: str, sha256: str) -> None:
        """Copy an original and its figures into another team's area (a hard link
        where the volume allows, else a copy). Existing files are kept."""
        src = self.original_path(src_team, sha256)
        dest = self.original_path(dest_team, sha256)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.exists(dest):
            _link_or_copy(src, dest)
        src_figs = os.path.join(self._team_dir(src_team), "figures", sha256)
        dest_figs = os.path.join(self._team_dir(dest_team), "figures", sha256)
        if os.path.isdir(src_figs):
            os.makedirs(dest_figs, exist_ok=True)
            for name in os.listdir(src_figs):
                target = os.path.join(dest_figs, name)
                if not os.path.exists(target):
                    _link_or_copy(os.path.join(src_figs, name), target)

    def delete_team(self, team: str) -> bool:
        """Remove everything stored for a team. Returns False if nothing was there."""
        path = self._team_dir(team)
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
        return True

    async def delete_original(self, team: str, sha256: str) -> bool:
        """Remove a stored original. Returns False if it was not there."""
        path = self.original_path(team, sha256)
        try:
            await asyncio.to_thread(os.remove, path)
        except FileNotFoundError:
            return False
        return True


def _unlink_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _link_or_copy(src: str, dest: str) -> None:
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
