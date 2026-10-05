"""Process-wide store handles, set by the app lifespan. `State` holds them as class
attributes, so tests can replace one with a fake.
"""
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
    admin: AdminStore
