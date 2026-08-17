"""KWIM service - the framework-agnostic contract (docs/contract.md).

Assembles the process: opens the stores, starts the background consumers, mounts
the routers. The contract surface itself is one module per concern under
`kwim_api/routers/`.

Run (dev):  uvicorn kwim_api.main:app
  env: KWIM_PG_DSN, KWIM_FALKOR_URL, KWIM_RABBITMQ_URL, KWIM_API_KEYS="devkey:acme"
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import otel
from .embedder import Embedder
from .gate import Gate
from .routers import ALL_ROUTERS
from .runtime import State
from .semantic_consumer import SemanticConsumer
from .stores.bus import Bus
from .stores.falkor import FalkorStore
from .stores.postgres import PostgresStore

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    State.pg = PostgresStore()
    State.falkor = FalkorStore()
    State.bus = Bus()
    State.embedder = Embedder()
    await State.pg.connect()
    await State.falkor.connect()
    await State.bus.connect()
    # Gate consumes proposals on its own channel within this process; split into
    # its own deployment if it needs to scale independently.
    gate_channel = await State.bus._conn.channel()
    app.state.gate = Gate(State.pg, State.falkor, gate_channel, State.embedder)
    await app.state.gate.run()
    # Semantic consumer mirrors the gate: durable queue on kwim.*.episodic,
    # embeds text events and writes :SemanticItem nodes.
    semantic_channel = await State.bus._conn.channel()
    app.state.semantic_consumer = SemanticConsumer(State.falkor, State.embedder, semantic_channel)
    await app.state.semantic_consumer.run()
    try:
        yield
    finally:
        await State.bus.close()
        await State.falkor.close()
        await State.pg.close()
        await State.embedder.close()


app = FastAPI(
    title="KWIM", version="0.2.0",
    summary="Knowledge - Wisdom - Intelligence - Memory - the contract teams code against.",
    lifespan=lifespan,
)
otel.configure(app)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "service": "kwim", "version": app.version}


for r in ALL_ROUTERS:
    app.include_router(r)
