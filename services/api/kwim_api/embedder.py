"""Async HTTP client for the TEI embedder service."""

import httpx

from .config import settings


class Embedder:
    def __init__(self) -> None:
        self._url = settings.embedder_url
        self._http = httpx.AsyncClient(timeout=settings.embed_timeout_s)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text, returning one vector per input in the same order.
        Raises if the response length differs, since callers pair by position."""
        r = await self._http.post(f"{self._url}/embed", json={"inputs": texts})
        r.raise_for_status()
        vectors = r.json()
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} inputs")
        return vectors

    async def close(self) -> None:
        await self._http.aclose()
