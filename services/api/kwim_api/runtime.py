"""Process-wide store handles, set by the app lifespan. `State` holds them as class
attributes, so tests can replace one with a fake.
"""
from typing import Any

from .embedder import Embedder
from .stores.admin import AdminStore
from .stores.bus import Bus
from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore


class State:
    pg: PostgresStore
    falkor: FalkorStore
    bus: Bus
    embedder: Embedder
    # The document embedder; None when KWIM_DOCS_EMBEDDER_URL is unset.
    docs_embedder: Embedder | None = None
    # The document reranker (documents.rerank.Reranker); None when unset.
    docs_reranker: Any = None
    admin: AdminStore
