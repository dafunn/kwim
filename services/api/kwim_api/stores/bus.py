"""RabbitMQ publisher - the async write path. Proposals and episodic events are
published to one topic exchange on the /kwim vhost, with routing keys
kwim.<team>.<kind>.proposed and kwim.<team>.episodic.
"""
import json
from typing import Any

import aio_pika

from ..config import settings

_EXCHANGE = settings.rmq_exchange


class Bus:
    def __init__(self) -> None:
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._ch: aio_pika.abc.AbstractChannel | None = None
        self._ex: aio_pika.abc.AbstractExchange | None = None

    async def connect(self) -> None:
        # Discrete arguments, not a URL; see docs/DESIGN.md, "Configuration".
        self._conn = await aio_pika.connect_robust(
            host=settings.rmq_host, port=settings.rmq_port, login=settings.rmq_user,
            password=settings.rmq_password, virtualhost=settings.rmq_vhost,
        )
        self._ch = await self._conn.channel()
        self._ex = await self._ch.declare_exchange(_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def publish(self, team: str, kind: str, body: dict[str, Any]) -> None:
        """kind e.g. 'knowledge.proposed', 'wisdom.proposed', 'episodic'."""
        routing_key = f"kwim.{team}.{kind}"
        await self._ex.publish(
            aio_pika.Message(
                body=json.dumps(body).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )
