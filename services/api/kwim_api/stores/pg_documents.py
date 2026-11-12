"""Document tables in a team's schema: documents, document_revisions,
document_figures. `PgDocumentStore` is mixed into `PostgresStore` and uses its
pool.
"""
import json
from typing import Any

from psycopg.rows import dict_row

from .pg_pool import cursor, is_uuid

_DOC_COLUMNS = ("id, title, tags, status, current_revision, created_at, created_by, "
                "updated_at, promoted_to, promoted_from_team, promoted_from_id")
_REV_COLUMNS = ("id, document_id, revision_number, label, sha256, mime_type, filename, "
                "byte_size, page_count, extractor, extractor_version, status, error, "
                "uploaded_at, uploaded_by, ingested_at")


class PgDocumentStore:
    _pool: Any

    @staticmethod
    def _doc_schema(team: str) -> str:
        from .postgres import _schema
        return _schema(team)

    async def create_document(self, team: str, *, title: str, tags: list[str],
                              created_by: str | None) -> dict[str, Any]:
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO {s}.documents (title, tags, created_by) "
                f"VALUES (%s, %s, %s) RETURNING {_DOC_COLUMNS}",
                (title, json.dumps(tags), created_by),
            )
            return await cur.fetchone()

    async def add_revision(self, team: str, *, document_id: str, sha256: str,
                           mime_type: str, filename: str | None, byte_size: int,
                           label: str | None, uploaded_by: str | None) -> dict[str, Any]:
        """Insert the next revision of a document, numbered one past its highest."""
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO {s}.document_revisions "
                "(document_id, revision_number, label, sha256, mime_type, filename, "
                " byte_size, uploaded_by) "
                "SELECT %s, COALESCE(MAX(revision_number), 0) + 1, %s, %s, %s, %s, %s, %s "
                f"FROM {s}.document_revisions WHERE document_id = %s "
                f"RETURNING {_REV_COLUMNS}",
                (document_id, label, sha256, mime_type, filename, byte_size, uploaded_by,
                 document_id),
            )
            return await cur.fetchone()

    async def get_document(self, team: str, document_id: str) -> dict[str, Any] | None:
        """One document with its revisions, newest first. None for an unknown
        or malformed id."""
        if not is_uuid(document_id):
            return None
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_DOC_COLUMNS} FROM {s}.documents WHERE id = %s", (document_id,))
            doc = await cur.fetchone()
            if doc is None:
                return None
            await cur.execute(
                f"SELECT {_REV_COLUMNS} FROM {s}.document_revisions "
                "WHERE document_id = %s ORDER BY revision_number DESC",
                (document_id,),
            )
            doc["revisions"] = list(await cur.fetchall())
            return doc

    async def get_revision(self, team: str, revision_id: str) -> dict[str, Any] | None:
        if not is_uuid(revision_id):
            return None
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_REV_COLUMNS} FROM {s}.document_revisions WHERE id = %s",
                (revision_id,))
            return await cur.fetchone()

    async def list_documents(self, team: str, *, limit: int = 100,
                             offset: int = 0) -> list[dict[str, Any]]:
        """Documents, newest first."""
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_DOC_COLUMNS} FROM {s}.documents "
                "ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return list(await cur.fetchall())

    async def finish_revision(self, team: str, revision_id: str, *, status: str,
                              error: str | None = None,
                              extracted: dict[str, Any] | None = None,
                              extractor: str | None = None,
                              extractor_version: str | None = None,
                              page_count: int | None = None) -> None:
        """Record an ingest outcome. A ready revision becomes the document's
        current revision and makes the document ready. A failed revision fails
        the document only when it has no ready revision."""
        s = self._doc_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.document_revisions SET status = %s, error = %s, "
                "extracted = COALESCE(%s, extracted), extractor = COALESCE(%s, extractor), "
                "extractor_version = COALESCE(%s, extractor_version), "
                "page_count = COALESCE(%s, page_count), "
                "ingested_at = CASE WHEN %s = 'ready' THEN now() ELSE ingested_at END "
                "WHERE id = %s",
                (status, error, json.dumps(extracted) if extracted is not None else None,
                 extractor, extractor_version, page_count, status, revision_id),
            )
            if status == "ready":
                await cur.execute(
                    f"UPDATE {s}.documents d SET current_revision = r.id, "
                    "status = CASE WHEN d.status = 'retracted' THEN d.status ELSE 'ready' END, "
                    "updated_at = now() "
                    f"FROM {s}.document_revisions r WHERE r.id = %s AND d.id = r.document_id",
                    (revision_id,),
                )
            else:
                await cur.execute(
                    f"UPDATE {s}.documents d SET status = 'failed', updated_at = now() "
                    f"FROM {s}.document_revisions r WHERE r.id = %s AND d.id = r.document_id "
                    "AND d.current_revision IS NULL AND d.status = 'ingesting'",
                    (revision_id,),
                )

    async def set_document_ingesting(self, team: str, document_id: str) -> None:
        """A new upload on a document with no ready revision puts it back to ingesting."""
        s = self._doc_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.documents SET status = 'ingesting', updated_at = now() "
                "WHERE id = %s AND status = 'failed'",
                (document_id,),
            )

    async def prune_revisions(self, team: str, document_id: str,
                              keep: int) -> list[str]:
        """Delete all but the newest `keep` revisions of a document, never its
        current revision. Returns the sha256 of each deleted revision."""
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"DELETE FROM {s}.document_revisions r "
                "WHERE r.document_id = %s "
                f"AND r.id IS DISTINCT FROM (SELECT current_revision FROM {s}.documents "
                "                             WHERE id = %s) "
                "AND r.id NOT IN ("
                f"    SELECT id FROM {s}.document_revisions WHERE document_id = %s "
                "    ORDER BY revision_number DESC LIMIT %s) "
                "RETURNING r.sha256",
                (document_id, document_id, document_id, max(keep, 1)),
            )
            return [row["sha256"] for row in await cur.fetchall()]

    async def sha256_in_use(self, team: str, sha256: str) -> bool:
        """Whether any revision in the team still refers to this file."""
        s = self._doc_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"SELECT 1 FROM {s}.document_revisions WHERE sha256 = %s LIMIT 1",
                (sha256,))
            return await cur.fetchone() is not None

    async def replace_figures(self, team: str, revision_id: str,
                              figures: list[dict[str, Any]]) -> None:
        """Set a revision's figures (id, page, caption, object_key)."""
        s = self._doc_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"DELETE FROM {s}.document_figures WHERE revision_id = %s", (revision_id,))
            for fig in figures:
                await cur.execute(
                    f"INSERT INTO {s}.document_figures "
                    "(revision_id, figure_id, page, caption, object_key) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (revision_id, fig["id"], fig.get("page"), fig.get("caption"),
                     fig["object_key"]),
                )

    async def get_current_extracted(self, team: str,
                                    document_id: str) -> dict[str, Any] | None:
        """The current revision of a document with its extracted structure."""
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT r.id, r.revision_number, r.mime_type, r.page_count, r.sha256, "
                f"r.extracted FROM {s}.documents d "
                f"JOIN {s}.document_revisions r ON r.id = d.current_revision "
                "WHERE d.id = %s",
                (document_id,))
            return await cur.fetchone()

    async def list_projectable(self, team: str) -> dict[str, tuple[str, str | None]]:
        """Documents that should be in the docs graph (ready, with a current
        revision): id -> (current revision id, promoted_to), oldest first."""
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT id, current_revision, promoted_to FROM {s}.documents "
                "WHERE status = 'ready' AND current_revision IS NOT NULL ORDER BY created_at")
            return {str(r["id"]): (str(r["current_revision"]),
                                   str(r["promoted_to"]) if r["promoted_to"] else None)
                    for r in await cur.fetchall()}

    async def set_document_status(self, team: str, document_id: str, status: str) -> None:
        s = self._doc_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(
                f"UPDATE {s}.documents SET status = %s, updated_at = now() WHERE id = %s",
                (status, document_id))

    async def document_files(self, team: str, document_id: str) -> dict[str, Any]:
        """What forgetting a document removes: revision and figure counts, total
        bytes, and the sha256 of each revision."""
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT count(*) AS revisions, COALESCE(sum(byte_size), 0) AS bytes, "
                "COALESCE(array_agg(DISTINCT sha256), '{}') AS shas "
                f"FROM {s}.document_revisions WHERE document_id = %s", (document_id,))
            row = await cur.fetchone()
            await cur.execute(
                f"SELECT count(*) AS n FROM {s}.document_figures f "
                f"JOIN {s}.document_revisions r ON r.id = f.revision_id "
                "WHERE r.document_id = %s", (document_id,))
            figures = (await cur.fetchone())["n"]
            return {"revisions": row["revisions"], "bytes": int(row["bytes"]),
                    "shas": list(row["shas"]), "figures": figures}

    async def delete_document(self, team: str, document_id: str) -> bool:
        """Delete a document and, by cascade, its revisions and figures."""
        s = self._doc_schema(team)
        async with cursor(self._pool) as cur:
            await cur.execute(f"DELETE FROM {s}.documents WHERE id = %s", (document_id,))
            return cur.rowcount > 0

    async def count_documents(self, team: str) -> dict[str, int]:
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT (SELECT count(*) FROM {s}.documents) AS documents, "
                f"(SELECT COALESCE(sum(byte_size), 0) FROM {s}.document_revisions) AS bytes")
            row = await cur.fetchone()
            return {"documents": row["documents"], "bytes": int(row["bytes"])}

    async def copy_document(self, src_team: str, document_id: str, dest_team: str, *,
                            created_by: str | None) -> dict[str, Any]:
        """Copy a document's current revision (with its extracted structure and
        figures) into another team's schema as a new document, recording where it
        came from. The copy's revision starts as ingesting until it is projected.
        Returns {"document_id", "revision_id", "sha256"}."""
        src = self._doc_schema(src_team)
        dst = self._doc_schema(dest_team)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"INSERT INTO {dst}.documents (title, tags, created_by, "
                        "promoted_from_team, promoted_from_id) "
                        f"SELECT title, tags, %s, %s, id FROM {src}.documents WHERE id = %s "
                        "RETURNING id", (created_by, src_team, document_id))
                    new_doc = (await cur.fetchone())["id"]
                    await cur.execute(
                        f"INSERT INTO {dst}.document_revisions (document_id, revision_number, "
                        "label, sha256, mime_type, filename, byte_size, page_count, extractor, "
                        "extractor_version, extracted, uploaded_by) "
                        "SELECT %s, 1, r.label, r.sha256, r.mime_type, r.filename, r.byte_size, "
                        "r.page_count, r.extractor, r.extractor_version, r.extracted, %s "
                        f"FROM {src}.documents d JOIN {src}.document_revisions r "
                        "ON r.id = d.current_revision WHERE d.id = %s "
                        "RETURNING id, sha256, (SELECT current_revision FROM "
                        f"{src}.documents WHERE id = %s) AS src_revision",
                        (new_doc, created_by, document_id, document_id))
                    rev = await cur.fetchone()
                    await cur.execute(
                        f"INSERT INTO {dst}.document_figures (revision_id, figure_id, page, "
                        "caption, object_key) SELECT %s, figure_id, page, caption, object_key "
                        f"FROM {src}.document_figures WHERE revision_id = %s",
                        (rev["id"], rev["src_revision"]))
                    await cur.execute(
                        f"UPDATE {src}.documents SET promoted_to = %s, updated_at = now() "
                        "WHERE id = %s", (new_doc, document_id))
        return {"document_id": str(new_doc), "revision_id": str(rev["id"]),
                "sha256": rev["sha256"]}

    async def list_figures(self, team: str, revision_id: str) -> list[dict[str, Any]]:
        s = self._doc_schema(team)
        async with cursor(self._pool, row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT figure_id, page, caption, object_key FROM {s}.document_figures "
                "WHERE revision_id = %s ORDER BY figure_id", (revision_id,))
            return list(await cur.fetchall())
