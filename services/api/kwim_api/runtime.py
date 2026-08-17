"""Process-wide store handles, bound once by the app lifespan.

Separate from `main` so a router can reach the stores without importing the module
that imports it.

`State` is a namespace of class attributes, not an instance: the lifespan assigns
them at startup and every router reads the same class object, so a test can swap a
single attribute for a fake without rebuilding the app.
"""
from .embedder import Embedder
from .stores.bus import Bus
from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore


class State:
    pg: PostgresStore
    falkor: FalkorStore
    bus: Bus
    embedder: Embedder
