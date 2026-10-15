"""Async HTTP client for the TEI embedder service."""

import httpx

from .config import settings


class Embedder:
    def __init__(self) -> None:
        self._url = settings.embedder_url
        self._http = httpx.AsyncClient(timeout=settings.embed_timeout_s)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text, returning one vector per input in the same order.
        Sends at most `embedder_max_batch` inputs per request, since the embedder
        refuses larger batches. Raises if any response length differs, since
        callers pair by position."""
        size = max(1, settings.embedder_max_batch)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            vectors.extend(await self._embed_batch(texts[start:start + size]))
        return vectors

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        r = await self._http.post(f"{self._url}/embed", json={"inputs": texts})
        r.raise_for_status()
        vectors = r.json()
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} inputs")
        return vectors

    async def close(self) -> None:
        await self._http.aclose()
