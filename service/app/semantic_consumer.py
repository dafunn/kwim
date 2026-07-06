"""Bus consumer that embeds episodic events carrying text into :SemanticItem nodes.

Mirrors the gate pattern: durable queue, in-process, started from lifespan.
For each episodic event with a non-empty `text` field, we embed it and write a
:SemanticItem to the team's graph. Idempotent on event_id + MERGE.

Embed-failure handling: the durable record is already in Postgres, so on
embedder error we log and ack. A backfill/re-embed job is the recovery path.
"""
import json
import logging
from typing import Any

import aio_pika

from .embedder import Embedder
from .stores.bus import _EXCHANGE
from .stores.falkor import FalkorStore

logger = logging.getLogger(__name__)


class SemanticConsumer:
    def __init__(
        self,
        falkor: FalkorStore,
        embedder: Embedder,
        bus_channel: aio_pika.abc.AbstractChannel,
    ) -> None:
        self._falkor = falkor
        self._embedder = embedder
        self._ch = bus_channel

    async def run(self) -> None:
        """Bind a durable queue to kwim.*.episodic and consume."""
        ex = await self._ch.declare_exchange(_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        q = await self._ch.declare_queue("kwim.semantic", durable=True)
        await q.bind(ex, routing_key="kwim.*.episodic")
        await q.consume(self._on_message)

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            payload = json.loads(message.body.decode())
            # Team is encoded in the routing key: kwim.<team>.episodic
            routing_key = message.routing_key or ""
            parts = routing_key.split(".")
            team = parts[1] if len(parts) >= 2 else None
            await self.handle(team, payload)

    async def handle(self, team: str | None, payload: dict[str, Any]) -> None:
        if not team:
            logger.warning("semantic_consumer: could not parse team from routing key")
            return

        event_data = payload.get("event_data", {})
        text = event_data.get("text")
        if not isinstance(text, str) or not text.strip():
            # Skip events without meaningful text (opt-in; keeps the index free
            # of health-check/structural noise).
            return

        event_id = payload.get("event_id")
        if not event_id:
            logger.warning("semantic_consumer: missing event_id for team=%s", team)
            return

        try:
            vectors = await self._embedder.embed([text])
        except Exception:
            # Embedder failure: log and ack. Durable source is in Postgres;
            # recovery is a backfill job (deferred).
            logger.exception("semantic_consumer: embedder failed for event_id=%s", event_id)
            return

        metadata: dict[str, Any] = {
            "event_id": event_id,
            "event_type": payload.get("event_type"),
            "agent_id": payload.get("agent_id"),
            "session_id": payload.get("session_id"),
            "occurred_at": payload.get("occurred_at"),
        }
        # Drop None values for cleaner metadata.
        metadata = {k: v for k, v in metadata.items() if v is not None}

        await self._falkor.materialize_semantic(
            team,
            {
                "id": event_id,
                "content": text,
                "embedding": vectors[0],
                "metadata": metadata,
            },
        )
