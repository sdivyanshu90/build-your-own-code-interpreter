"""Result and job-record persistence.

Writes the hot copy (status + result) to Redis for fast polling and streams a durable cold copy
of each result to MinIO. Job-record updates are read-modify-write with the TTL preserved so the
API's view of a job stays consistent through its lifecycle.

The MinIO client is synchronous; calls are offloaded to a thread so they never block the event
loop. MinIO is treated as best-effort: if it is unavailable, the hot Redis tier still serves
results and a warning is logged.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from worker.sandbox import constants
from worker.sandbox.types import ExecutionResult

logger = logging.getLogger("sandbox.worker.storage")


def _now_iso() -> str:
    """Current UTC time in RFC 3339."""
    return datetime.now(timezone.utc).isoformat()


class ResultStore:
    """Persists job records and execution results across Redis and MinIO."""

    def __init__(self, redis: aioredis.Redis[Any], minio_client: Any | None, bucket: str) -> None:
        self._redis = redis
        self._minio = minio_client
        self._bucket = bucket

    async def ensure_bucket(self) -> None:
        """Create the artifact bucket if it does not exist (best effort)."""
        if self._minio is None:
            return
        try:
            exists = await asyncio.to_thread(self._minio.bucket_exists, self._bucket)
            if not exists:
                await asyncio.to_thread(self._minio.make_bucket, self._bucket)
        except Exception as exc:  # pragma: no cover - infra dependent
            logger.warning("minio bucket ensure failed", extra={"err": str(exc)})

    async def get_record(self, job_id: str) -> dict[str, Any] | None:
        """Fetch the current JobRecord, or None if it has expired/never existed."""
        raw = await self._redis.get(constants.job_record_key(job_id))
        if raw is None:
            return None
        try:
            record: dict[str, Any] = json.loads(raw)
            return record
        except json.JSONDecodeError:
            return None

    async def is_cancelled(self, job_id: str) -> bool:
        """True if the API has signalled cancellation for this job."""
        return bool(await self._redis.exists(constants.cancel_key(job_id)))

    async def patch_record(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        """Read-modify-write the JobRecord, preserving its TTL. Returns the new record."""
        record = await self.get_record(job_id)
        if record is None:
            return None
        record.update(fields)
        record["updated_at"] = _now_iso()
        await self._redis.set(constants.job_record_key(job_id), json.dumps(record), keepttl=True)
        return record

    async def set_running(self, job_id: str, worker_id: str) -> None:
        """Transition a job to RUNNING and stamp the owning worker."""
        await self.patch_record(job_id, status=constants.STATUS_RUNNING, worker_id=worker_id)

    async def store_terminal(self, job_id: str, result: ExecutionResult, worker_id: str) -> None:
        """Persist a terminal result to Redis (hot) and MinIO (cold), and finalise the record."""
        await self._archive(job_id, result)
        result_dict = result.to_dict()
        await self._redis.set(
            constants.result_key(job_id),
            json.dumps(result_dict),
            ex=constants.RESULT_TTL_SECONDS,
        )
        await self.patch_record(
            job_id, status=result.status, result=result_dict, worker_id=worker_id
        )

    async def _archive(self, job_id: str, result: ExecutionResult) -> None:
        """Upload a durable copy of the result JSON to MinIO (best effort)."""
        if self._minio is None:
            return
        body = json.dumps(result.to_dict()).encode("utf-8")
        try:
            await asyncio.to_thread(
                self._minio.put_object,
                self._bucket,
                f"results/{job_id}.json",
                io.BytesIO(body),
                len(body),
                "application/json",
            )
        except Exception as exc:  # pragma: no cover - infra dependent
            logger.warning("minio archive failed", extra={"job_id": job_id, "err": str(exc)})
