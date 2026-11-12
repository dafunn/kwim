"""Async client for a TEI reranker (/rerank)."""
from typing import Any

import httpx

from ..config import settings


class Reranker:
    def __init__(self, url: str, timeout_s: float | None = None,
                 max_batch: int | None = None) -> None:
        self._url = url
        self._max_batch = max(1, max_batch or settings.documents_reranker_max_batch)
        self._http = httpx.AsyncClient(
            timeout=timeout_s or settings.documents_reranker_timeout_s)

    async def scores(self, query: str, texts: list[str]) -> list[float]:
        """One relevance score per text, in input order. Long inputs are truncated."""
        out: list[float] = [0.0] * len(texts)
        for start in range(0, len(texts), self._max_batch):
            batch = texts[start:start + self._max_batch]
            r = await self._http.post(f"{self._url}/rerank", json={
                "query": query, "texts": batch, "truncate": True, "raw_scores": True})
            r.raise_for_status()
            items: list[dict[str, Any]] = r.json()
            if len(items) != len(batch):
                raise ValueError(f"reranker returned {len(items)} scores for {len(batch)} texts")
            for item in items:
                out[start + item["index"]] = float(item["score"])
        return out

    async def close(self) -> None:
        await self._http.aclose()


def document_reranker() -> Reranker | None:
    """The reranker, or None when KWIM_DOCS_RERANKER_URL is unset."""
    if not settings.documents_reranker_url:
        return None
    return Reranker(settings.documents_reranker_url)
