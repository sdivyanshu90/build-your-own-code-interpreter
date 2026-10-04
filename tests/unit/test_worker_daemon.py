"""Unit tests for the worker daemon's job lifecycle and the stream consumer's bookkeeping.

These use in-memory fakes (no Redis, no Docker) to pin the behaviour of the at-least-once
pipeline: acked entries are deleted, poison messages reach a terminal state, concurrency slots
are released, and the consume loop never reads more than it can start.
"""

from __future__ import annotations

import asyncio
from typing import Any

from worker.queue.consumer import DEAD_LETTER_MAXLEN, QueueConsumer, QueueMessage
from worker.sandbox import constants
from worker.sandbox.types import ExecutionResult
from worker.worker import WorkerDaemon


class RecordingRedis:
    """Records the stream commands the consumer issues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        async def _call(*args: Any, **kwargs: Any) -> int:
            self.calls.append((name, args, kwargs))
            return 1

        return _call


class FakeStore:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.terminal: dict[str, ExecutionResult] = {}
        self.released: list[tuple[str, str]] = []

    async def is_cancelled(self, _job_id: str) -> bool:
        return self.cancelled

    async def store_terminal(self, job_id: str, result: ExecutionResult, _worker: str) -> None:
        self.terminal[job_id] = result

    async def release_slot(self, user_id: str, job_id: str) -> None:
        self.released.append((user_id, job_id))


class FakePublisher:
    def __init__(self) -> None:
        self.done_calls: list[str] = []

    async def done(self, job_id: str, _result: ExecutionResult) -> None:
        self.done_calls.append(job_id)


class FakeConsumer:
    def __init__(self) -> None:
        self.acked: list[str] = []
        self.dead: list[tuple[str, str]] = []
        self.read_counts: list[int] = []
        self.claim_counts: list[int] = []

    async def ack(self, msg_id: str) -> None:
        self.acked.append(msg_id)

    async def dead_letter(self, message: QueueMessage, reason: str) -> None:
        self.dead.append((message.msg_id, reason))

    async def queue_depth(self) -> int:
        return 0

    async def claim_stale(self, _idle: int, count: int, _retries: int) -> list[QueueMessage]:
        self.claim_counts.append(count)
        return []

    async def read(self, count: int, _block: int) -> list[QueueMessage]:
        self.read_counts.append(count)
        await asyncio.sleep(0.005)  # yield, as a blocking XREADGROUP would
        return []


def _daemon(worker_config, **fakes: Any) -> WorkerDaemon:
    daemon = WorkerDaemon(worker_config)
    for name, fake in fakes.items():
        setattr(daemon, name, fake)
    return daemon


def _message(delivery_count: int = 1, **payload: Any) -> QueueMessage:
    base: dict[str, Any] = {
        "job_id": "JOB1",
        "user_id": "u1",
        "language": "python",
        "request": {"language": "python", "code": "print(1)"},
    }
    base.update(payload)
    return QueueMessage(msg_id="1-0", payload=base, delivery_count=delivery_count)


class TestConsumerBookkeeping:
    async def test_ack_acks_and_deletes_the_entry(self):
        redis = RecordingRedis()
        consumer = QueueConsumer(redis, consumer_name="c")  # type: ignore[arg-type]
        await consumer.ack("5-0")
        assert [c[0] for c in redis.calls] == ["xack", "xdel"]
        assert redis.calls[1][1] == (constants.JOB_STREAM, "5-0")

    async def test_dead_letter_stream_is_capped(self):
        redis = RecordingRedis()
        consumer = QueueConsumer(redis, consumer_name="c")  # type: ignore[arg-type]
        await consumer.dead_letter(_message(), "boom")
        xadd = next(c for c in redis.calls if c[0] == "xadd")
        assert xadd[2]["maxlen"] == DEAD_LETTER_MAXLEN
        assert xadd[2]["approximate"] is True


class TestPoisonMessages:
    async def test_max_retries_exceeded_reaches_a_terminal_failed_state(self, worker_config):
        store, consumer, pub = FakeStore(), FakeConsumer(), FakePublisher()
        daemon = _daemon(worker_config, store=store, consumer=consumer, publisher=pub)
        await daemon._process(_message(delivery_count=worker_config.max_retries + 1))
        result = store.terminal["JOB1"]
        assert result.status == constants.STATUS_FAILED
        assert "repeated delivery" in result.stderr
        assert consumer.dead == [("1-0", "max_retries_exceeded")]
        assert store.released == [("u1", "JOB1")]
        assert pub.done_calls == ["JOB1"]

    async def test_malformed_payload_is_finalized_and_dead_lettered(self, worker_config):
        store, consumer, pub = FakeStore(), FakeConsumer(), FakePublisher()
        daemon = _daemon(worker_config, store=store, consumer=consumer, publisher=pub)
        await daemon._process(_message(request="not-a-dict"))
        assert store.terminal["JOB1"].status == constants.STATUS_FAILED
        assert consumer.dead == [("1-0", "malformed_payload")]


class TestSlotRelease:
    async def test_slot_released_when_job_finishes(self, worker_config):
        store, consumer = FakeStore(), FakeConsumer()
        daemon = _daemon(worker_config, store=store, consumer=consumer)

        async def fake_run_job(_job_id: str, _request: Any) -> None:
            return None

        daemon._run_job = fake_run_job  # type: ignore[method-assign]
        await daemon._process(_message())
        assert store.released == [("u1", "JOB1")]
        assert consumer.acked == ["1-0"]

    async def test_slot_released_when_cancelled_before_pickup(self, worker_config):
        store, consumer, pub = FakeStore(cancelled=True), FakeConsumer(), FakePublisher()
        daemon = _daemon(worker_config, store=store, consumer=consumer, publisher=pub)
        await daemon._process(_message())
        assert store.terminal["JOB1"].status == constants.STATUS_KILLED
        assert store.released == [("u1", "JOB1")]


class TestConsumeLoopBackpressure:
    async def test_reads_no_more_than_free_slots(self, worker_config):
        # Regression: the loop used to read read_count (10) messages with only 2 pool slots,
        # leaving delivered-but-idle entries that other workers would steal and run twice.
        consumer = FakeConsumer()
        daemon = _daemon(worker_config, consumer=consumer)
        assert worker_config.worker_concurrency == 2 < worker_config.read_count

        async def stop_soon() -> None:
            await asyncio.sleep(0.05)
            daemon.request_stop()

        await asyncio.gather(daemon._consume_loop(), stop_soon())
        assert consumer.read_counts and max(consumer.read_counts) <= 2
        assert consumer.claim_counts and max(consumer.claim_counts) <= 2

    async def test_does_not_read_when_pool_is_full(self, worker_config):
        consumer = FakeConsumer()
        daemon = _daemon(worker_config, consumer=consumer)
        daemon._inflight.update(
            {asyncio.ensure_future(asyncio.sleep(1)) for _ in range(2)}  # type: ignore[arg-type]
        )

        async def stop_soon() -> None:
            await asyncio.sleep(0.1)
            daemon.request_stop()

        await asyncio.gather(daemon._consume_loop(), stop_soon())
        assert consumer.read_counts == []
        for task in list(daemon._inflight):
            task.cancel()
