"""Shared protocol constants.

These mirror the API server's `REDIS_KEYS` and status enums (api/src/types/index.ts) exactly.
Any change here must be reflected there — the two services communicate solely through these
key names and string values.
"""

from __future__ import annotations

from typing import Final

# ── Redis stream / group ────────────────────────────────────────────────────────────────
JOB_STREAM: Final[str] = "sandbox:jobs"
DEAD_LETTER_STREAM: Final[str] = "sandbox:jobs:dead"
CONSUMER_GROUP: Final[str] = "workers"

# ── Result/record TTLs ──────────────────────────────────────────────────────────────────
RESULT_TTL_SECONDS: Final[int] = 3600
RECORD_TTL_SECONDS: Final[int] = 3600 * 2

# ── Job statuses (mirror api JOB_STATUSES) ──────────────────────────────────────────────
STATUS_PENDING: Final[str] = "PENDING"
STATUS_RUNNING: Final[str] = "RUNNING"
STATUS_COMPLETED: Final[str] = "COMPLETED"
STATUS_FAILED: Final[str] = "FAILED"
STATUS_TIMEOUT: Final[str] = "TIMEOUT"
STATUS_KILLED: Final[str] = "KILLED"

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMEOUT, STATUS_KILLED}
)


def job_record_key(job_id: str) -> str:
    """Redis key holding the full JobRecord JSON."""
    return f"sandbox:job:{job_id}"


def result_key(job_id: str) -> str:
    """Redis key holding the ExecutionResult JSON."""
    return f"sandbox:result:{job_id}"


def stream_channel(job_id: str) -> str:
    """Pub/Sub channel for live stdout/stderr of a job."""
    return f"sandbox:stream:{job_id}"


def cancel_key(job_id: str) -> str:
    """Key set by the API when a job is cancelled."""
    return f"sandbox:cancel:{job_id}"
