"""Live-output publisher.

Publishes stdout/stderr/status/done messages on a per-job Redis Pub/Sub channel. The API's
WebSocket handler subscribes to this channel and forwards the frames to the client. Message
shapes mirror the ``StreamMessage`` union in api/src/types/index.ts exactly.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from worker.sandbox import constants
from worker.sandbox.types import ExecutionResult


class StreamPublisher:
    """Publishes live execution events to a job's Pub/Sub channel."""

    def __init__(self, redis: aioredis.Redis[Any]) -> None:
        self._redis = redis

    async def _publish(self, job_id: str, message: dict[str, object]) -> None:
        await self._redis.publish(constants.stream_channel(job_id), json.dumps(message))

    async def output(self, job_id: str, kind: str, data: str) -> None:
        """Publish a chunk of stdout or stderr (``kind`` is 'stdout' or 'stderr')."""
        await self._publish(job_id, {"kind": kind, "data": data})

    async def status(self, job_id: str, status: str) -> None:
        """Publish a status transition."""
        await self._publish(job_id, {"kind": "status", "status": status})

    async def done(self, job_id: str, result: ExecutionResult) -> None:
        """Publish the terminal event with final metrics."""
        await self._publish(
            job_id,
            {
                "kind": "done",
                "exit_code": result.exit_code,
                "status": result.status,
                "wall_time_ms": result.wall_time_ms,
                "timed_out": result.timed_out,
                "oom_killed": result.oom_killed,
            },
        )
