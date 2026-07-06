"""Async HTTP client for the TEI embedder service."""

import httpx

from .config import settings


class Embedder:
    def __init__(self) -> None:
        self._url = settings.embedder_url
        self._http = httpx.AsyncClient(timeout=settings.embed_timeout_s)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        r = await self._http.post(f"{self._url}/embed", json={"inputs": texts})
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._http.aclose()
