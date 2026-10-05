"""The KWIM service: opens the stores, starts the gate and semantic consumers,
and mounts the routers (one module per surface under `kwim_api/routers/`).

    uvicorn kwim_api.main:app
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import otel
from .admin_auth import warn_if_insecure_cookie
from .embedder import Embedder
from .gate import Gate
from .routers import ALL_ROUTERS
from .runtime import State
from .semantic_consumer import SemanticConsumer
from .stores.admin import AdminStore
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
    State.admin = AdminStore()
    await State.pg.connect()
    await State.falkor.connect()
    await State.bus.connect()
    await State.admin.connect()
    # Job rows still `running` belong to a previous process; mark them failed.
    # Best-effort.
    try:
        n_orphaned = await State.admin.fail_orphaned_jobs()
        if n_orphaned:
            log.warning("marked %d orphaned running job(s) as failed at startup", n_orphaned)
    except Exception:
        log.warning("orphaned-job sweep failed at startup - continuing", exc_info=True)
    warn_if_insecure_cookie()
    # The gate consumes proposals on its own channel in this process.
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
        await State.admin.close()


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
