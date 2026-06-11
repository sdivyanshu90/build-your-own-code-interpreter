"""Worker daemon entrypoint.

Pulls jobs from the Redis stream, runs each in a sandbox, streams output, stores results, and
acknowledges — all under a bounded concurrency pool. Recovers stale jobs from dead workers,
dead-letters poison messages, reaps leaked containers, and shuts down gracefully (stop intake,
drain in-flight, close connections).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal

import redis.asyncio as aioredis
from minio import Minio

from worker.config import WorkerConfig, load_worker_config
from worker.queue.consumer import QueueConsumer, QueueMessage
from worker.queue.publisher import StreamPublisher
from worker.sandbox import constants
from worker.sandbox.cleanup import ContainerReaper
from worker.sandbox.errors import DockerUnavailableError, SandboxError, UnknownLanguageError
from worker.sandbox.executor import SandboxExecutor
from worker.sandbox.types import ExecutionRequest, ExecutionResult
from worker.storage.result_store import ResultStore
from worker.telemetry import metrics
from worker.telemetry.logging import configure_logging

# Poll interval for the per-job cancellation watcher.
_CANCEL_POLL_SECONDS = 0.5


class WorkerDaemon:
    """Owns the consume loop and the lifecycle of a single worker process."""

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.logger = configure_logging(config.log_level)
        self.redis: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
            config.redis_url, decode_responses=True
        )
        self.consumer = QueueConsumer(self.redis, consumer_name=config.worker_id)
        self.publisher = StreamPublisher(self.redis)
        self.store = ResultStore(self.redis, _build_minio(config), config.minio_bucket)
        self.executor = SandboxExecutor(config)
        self.reaper = ContainerReaper(config)
        self.semaphore = asyncio.Semaphore(config.worker_concurrency)
        self._stopping = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()
        self._docker_down_since: float | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Start background tasks and run the consume loop until shutdown."""
        await self.store.ensure_bucket()
        await self.consumer.ensure_group()
        reaper_task = asyncio.create_task(self.reaper.run_forever())
        self.logger.info("worker started", extra={"worker_id": self.config.worker_id})
        try:
            await self._consume_loop()
        finally:
            self.reaper.stop()
            await self._drain()
            with contextlib.suppress(Exception):
                reaper_task.cancel()
                await reaper_task
            await self.redis.aclose()
            self.logger.info("worker stopped", extra={"worker_id": self.config.worker_id})

    def request_stop(self) -> None:
        """Signal the consume loop to stop accepting new work."""
        self._stopping.set()

    async def _drain(self) -> None:
        """Wait for all in-flight jobs to finish before exiting."""
        if self._inflight:
            self.logger.info("draining in-flight jobs", extra={"count": len(self._inflight)})
            await asyncio.gather(*self._inflight, return_exceptions=True)

    # ── consume loop ────────────────────────────────────────────────────────────────────
    async def _consume_loop(self) -> None:
        """Continuously read new + reclaimed messages and dispatch them under the pool."""
        while not self._stopping.is_set():
            try:
                metrics.QUEUE_DEPTH.set(await self.consumer.queue_depth())
                stale = await self.consumer.claim_stale(
                    self.config.claim_min_idle_ms, self.config.read_count, self.config.max_retries
                )
                for message in stale:
                    metrics.RECLAIMED_JOBS.inc()
                    await self._dispatch(message)
                new = await self.consumer.read(self.config.read_count, self.config.block_ms)
                for message in new:
                    await self._dispatch(message)
            except aioredis.RedisError as exc:
                await self._backoff("redis", exc)

    async def _dispatch(self, message: QueueMessage) -> None:
        """Acquire a pool slot and schedule the job, tracking it for graceful drain."""
        await self.semaphore.acquire()
        task = asyncio.create_task(self._process(message))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        task.add_done_callback(lambda _t: self.semaphore.release())

    # ── per-job processing ──────────────────────────────────────────────────────────────
    async def _process(self, message: QueueMessage) -> None:
        """Run one job end-to-end with full error handling. Always acks unless retryable."""
        job_id = message.job_id
        if message.delivery_count > self.config.max_retries:
            await self.consumer.dead_letter(message, reason="max_retries_exceeded")
            metrics.DEAD_LETTERED.inc()
            return
        try:
            request = ExecutionRequest.from_dict(message.payload["request"])
        except (KeyError, TypeError, ValueError):
            await self.consumer.dead_letter(message, reason="malformed_payload")
            metrics.DEAD_LETTERED.inc()
            return

        if await self.store.is_cancelled(job_id):
            await self._finalize_cancelled(job_id, request.language)
            await self.consumer.ack(message.msg_id)
            return

        try:
            await self._run_job(job_id, request)
            await self.consumer.ack(message.msg_id)
        except DockerUnavailableError as exc:
            # Transient: do NOT ack — leave pending for reclaim after the daemon recovers.
            self.logger.error("docker unavailable", extra={"job_id": job_id, "err": str(exc)})
        except Exception as exc:
            await self._finalize_failed(job_id, request.language, exc)
            await self.consumer.ack(message.msg_id)

    async def _run_job(self, job_id: str, request: ExecutionRequest) -> None:
        """Execute the request, streaming output, and store the terminal result."""
        metrics.JOBS_IN_FLIGHT.inc()
        await self.store.set_running(job_id, self.config.worker_id)
        await self.publisher.status(job_id, constants.STATUS_RUNNING)
        cancel_event = asyncio.Event()
        watcher = asyncio.create_task(self._watch_cancel(job_id, cancel_event))

        async def on_output(kind: str, data: str) -> None:
            await self.publisher.output(job_id, kind, data)

        try:
            result = await self.executor.execute(
                request, job_id, on_output=on_output, cancel_event=cancel_event
            )
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            metrics.JOBS_IN_FLIGHT.dec()

        await self.store.store_terminal(job_id, result, self.config.worker_id)
        await self.publisher.done(job_id, result)
        self._record_metrics(request.language, result)
        self.logger.info(
            "job completed",
            extra={
                "job_id": job_id,
                "language": request.language,
                "status": result.status,
                "duration_ms": result.wall_time_ms,
                "exit_code": result.exit_code,
                "oom_killed": result.oom_killed,
                "timed_out": result.timed_out,
                "worker_id": self.config.worker_id,
            },
        )

    async def _watch_cancel(self, job_id: str, event: asyncio.Event) -> None:
        """Poll the cancellation flag and set the event when the API requests a kill."""
        try:
            while not event.is_set():
                if await self.store.is_cancelled(job_id):
                    event.set()
                    return
                await asyncio.sleep(_CANCEL_POLL_SECONDS)
        except asyncio.CancelledError:
            raise

    # ── result finalisation helpers ─────────────────────────────────────────────────────
    async def _finalize_cancelled(self, job_id: str, language: str) -> None:
        """Record a job that was cancelled before/at pickup."""
        result = ExecutionResult(job_id=job_id, status=constants.STATUS_KILLED)
        await self.store.store_terminal(job_id, result, self.config.worker_id)
        await self.publisher.done(job_id, result)
        self._record_metrics(language, result)

    async def _finalize_failed(self, job_id: str, language: str, exc: Exception) -> None:
        """Record an infrastructure/sandbox failure as a FAILED result with a safe message."""
        detail = (
            "unsupported language"
            if isinstance(exc, UnknownLanguageError)
            else (
                "sandbox failure" if isinstance(exc, SandboxError) else "internal execution error"
            )
        )
        self.logger.error(
            "job failed", extra={"job_id": job_id, "language": language, "err": str(exc)}
        )
        result = ExecutionResult(
            job_id=job_id, status=constants.STATUS_FAILED, stderr=detail, exit_code=None
        )
        await self.store.store_terminal(job_id, result, self.config.worker_id)
        await self.publisher.done(job_id, result)
        self._record_metrics(language, result)

    @staticmethod
    def _record_metrics(language: str, result: ExecutionResult) -> None:
        """Emit Prometheus metrics for a finished job."""
        metrics.EXECUTIONS_TOTAL.labels(language=language, status=result.status).inc()
        metrics.EXECUTION_DURATION.labels(language=language).observe(result.wall_time_ms / 1000)
        if result.oom_killed:
            metrics.OOM_KILLS.inc()
        if result.timed_out:
            metrics.TIMEOUT_KILLS.inc()

    async def _backoff(self, source: str, exc: Exception) -> None:
        """Exponential backoff with jitter on a transient infrastructure error."""
        delay = min(5.0, 0.5 * (2 ** random.randint(0, 3))) + random.uniform(0, 0.25)
        self.logger.warning(
            "transient error; backing off",
            extra={"source": source, "err": str(exc), "delay": delay},
        )
        await asyncio.sleep(delay)


def _build_minio(config: WorkerConfig) -> Minio | None:
    """Construct a MinIO client, or None if it cannot be configured."""
    try:
        return Minio(
            config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=config.minio_secure,
        )
    except Exception:  # pragma: no cover - defensive
        return None


async def _amain() -> None:
    """Async entrypoint: build the daemon, install signal handlers, run."""
    config = load_worker_config()
    metrics.start_metrics_server(config.metrics_port)
    daemon = WorkerDaemon(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.request_stop)

    await daemon.run()


def main() -> None:
    """Synchronous entrypoint for ``python -m worker.worker`` / the container CMD."""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
