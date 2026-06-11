"""Prometheus metrics for the worker.

Cardinality is bounded to (language, status) — never per-job. The HTTP exposition endpoint is
started on the configured port by :func:`start_metrics_server`.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

EXECUTIONS_TOTAL = Counter(
    "sandbox_executions_total",
    "Total sandbox executions by language and terminal status.",
    ["language", "status"],
)

EXECUTION_DURATION = Histogram(
    "sandbox_execution_duration_seconds",
    "Wall-clock execution duration by language.",
    ["language"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)

CONTAINER_STARTUP = Histogram(
    "sandbox_container_startup_seconds",
    "Time from job pickup to container start.",
    buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3, 5),
)

QUEUE_DEPTH = Gauge(
    "sandbox_queue_depth",
    "Approximate number of pending jobs in the stream (as seen by this worker).",
)

OOM_KILLS = Counter("sandbox_oom_kills_total", "Total executions terminated by the OOM killer.")

TIMEOUT_KILLS = Counter(
    "sandbox_timeout_kills_total", "Total executions terminated by the wall-clock timeout."
)

JOBS_IN_FLIGHT = Gauge("sandbox_jobs_in_flight", "Jobs currently executing on this worker.")

RECLAIMED_JOBS = Counter("sandbox_reclaimed_jobs_total", "Stale jobs reclaimed from dead workers.")

DEAD_LETTERED = Counter(
    "sandbox_dead_lettered_total", "Jobs moved to the dead-letter stream after exhausting retries."
)


def start_metrics_server(port: int) -> None:
    """Start the Prometheus metrics HTTP server on the given port."""
    start_http_server(port)
