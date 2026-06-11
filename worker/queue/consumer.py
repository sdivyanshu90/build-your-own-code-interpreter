"""Redis Streams consumer.

Provides the stream mechanics the worker loop is built on:

* ``ensure_group``   — create the consumer group + stream (idempotent).
* ``read``           — ``XREADGROUP`` of new messages with ``COUNT`` and ``BLOCK``.
* ``claim_stale``    — reclaim entries pending longer than ``min_idle_ms`` from dead workers
                       (``XPENDING`` extended + ``XCLAIM``), surfacing the delivery count so the
                       worker can dead-letter poison messages.
* ``ack``            — ``XACK`` (called only after the result is durably stored: at-least-once).
* ``dead_letter``    — move an entry to the dead-letter stream and ack it.

Concurrency control (the ``asyncio.Semaphore`` worker pool) and graceful shutdown live in
``worker.py``; this class is purely the stream interface so it can be unit-tested in isolation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from worker.sandbox import constants

logger = logging.getLogger("sandbox.worker.consumer")


@dataclass
class QueueMessage:
    """A claimed stream entry with its decoded payload."""

    msg_id: str
    payload: dict[str, Any]
    delivery_count: int = 1

    @property
    def job_id(self) -> str:
        return str(self.payload.get("job_id", ""))


@dataclass
class QueueConsumer:
    """Thin wrapper over Redis Streams for the worker's consumer group."""

    redis: aioredis.Redis
    consumer_name: str
    stream: str = constants.JOB_STREAM
    group: str = constants.CONSUMER_GROUP
    dead_letter_stream: str = constants.DEAD_LETTER_STREAM
    _group_ready: bool = field(default=False, init=False)

    async def ensure_group(self) -> None:
        """Create the consumer group (and the stream) if absent. Idempotent."""
        if self._group_ready:
            return
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            logger.info("consumer group created", extra={"stream": self.stream})
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def read(self, count: int, block_ms: int) -> list[QueueMessage]:
        """Read up to ``count`` new messages, blocking up to ``block_ms`` for arrivals."""
        await self.ensure_group()
        response = await self.redis.xreadgroup(
            self.group, self.consumer_name, {self.stream: ">"}, count=count, block=block_ms
        )
        if not response:
            return []
        messages: list[QueueMessage] = []
        for _stream, entries in response:
            for msg_id, fields in entries:
                messages.append(self._to_message(msg_id, fields, delivery_count=1))
        return messages

    async def claim_stale(
        self, min_idle_ms: int, count: int, max_retries: int
    ) -> list[QueueMessage]:
        """Reclaim entries idle longer than ``min_idle_ms`` from other (likely dead) consumers.

        Entries whose delivery count exceeds ``max_retries`` are returned with that count so the
        caller dead-letters them rather than re-running a poison message.
        """
        await self.ensure_group()
        pending = await self.redis.xpending_range(
            self.stream, self.group, min="-", max="+", count=count, idle=min_idle_ms
        )
        if not pending:
            return []
        claimed: list[QueueMessage] = []
        for entry in pending:
            msg_id = str(entry["message_id"])
            deliveries = int(entry["times_delivered"])
            result = await self.redis.xclaim(
                self.stream, self.group, self.consumer_name, min_idle_ms, [msg_id]
            )
            for claimed_id, fields in result:
                claimed.append(self._to_message(claimed_id, fields, delivery_count=deliveries))
        return claimed

    async def ack(self, msg_id: str) -> None:
        """Acknowledge an entry (after its result is durably stored)."""
        await self.redis.xack(self.stream, self.group, msg_id)

    async def dead_letter(self, message: QueueMessage, reason: str) -> None:
        """Move an entry to the dead-letter stream and ack it off the main stream."""
        await self.redis.xadd(
            self.dead_letter_stream,
            {
                "payload": json.dumps(message.payload),
                "reason": reason,
                "delivery_count": str(message.delivery_count),
            },
        )
        await self.ack(message.msg_id)
        logger.warning(
            "job dead-lettered",
            extra={
                "job_id": message.job_id,
                "reason": reason,
                "deliveries": message.delivery_count,
            },
        )

    async def queue_depth(self) -> int:
        """Approximate pending depth (stream length)."""
        try:
            return int(await self.redis.xlen(self.stream))
        except ResponseError:  # pragma: no cover - stream may not exist yet
            return 0

    @staticmethod
    def _to_message(msg_id: str, fields: dict[str, str], delivery_count: int) -> QueueMessage:
        """Decode a stream entry's ``payload`` field into a QueueMessage."""
        raw = fields.get("payload", "{}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        return QueueMessage(msg_id=str(msg_id), payload=payload, delivery_count=delivery_count)
