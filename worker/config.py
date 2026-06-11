"""Worker configuration parsed from the environment.

Validated once at startup. A misconfiguration raises immediately rather than failing later.
"""

from __future__ import annotations

import os
import socket
import tempfile
import uuid
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WorkerConfig:
    """Typed worker configuration."""

    redis_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    worker_concurrency: int
    worker_id: str
    docker_path: str
    # Path where the worker writes per-job code dirs (inside the worker container).
    local_workdir: str
    # The corresponding path as seen by the Docker daemon (host path for bind mounts).
    # Empty means "identical to local_workdir" (worker runs on the host).
    host_workdir: str
    block_ms: int
    read_count: int
    claim_min_idle_ms: int
    max_retries: int
    sigterm_grace_seconds: int
    metrics_port: int
    log_level: str
    otlp_endpoint: str

    def host_path_for(self, local_path: str) -> str:
        """Translate a worker-local path into the daemon-visible (host) path for bind mounts."""
        if not self.host_workdir:
            return local_path
        rel = os.path.relpath(local_path, self.local_workdir)
        return os.path.normpath(os.path.join(self.host_workdir, rel))


def load_worker_config() -> WorkerConfig:
    """Build the :class:`WorkerConfig` from environment variables."""
    default_workdir = os.path.join(tempfile.gettempdir(), "sandbox-work")
    return WorkerConfig(
        redis_url=_str("REDIS_URL", "redis://localhost:6379/0"),
        minio_endpoint=_str("MINIO_ENDPOINT", "localhost:9000"),
        minio_access_key=_str("MINIO_ACCESS_KEY", "minioadmin"),
        minio_secret_key=_str("MINIO_SECRET_KEY", "minioadmin"),
        minio_bucket=_str("MINIO_BUCKET", "sandbox-artifacts"),
        minio_secure=_bool("MINIO_USE_SSL", False),
        worker_concurrency=_int("WORKER_CONCURRENCY", 4),
        worker_id=_str("WORKER_ID", f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"),
        docker_path=_str("DOCKER_PATH", "docker"),
        local_workdir=_str("SANDBOX_WORKDIR", default_workdir),
        host_workdir=_str("SANDBOX_HOST_WORKDIR", ""),
        block_ms=_int("QUEUE_BLOCK_MS", 5000),
        read_count=_int("QUEUE_READ_COUNT", 10),
        claim_min_idle_ms=_int("QUEUE_CLAIM_MIN_IDLE_MS", 60000),
        max_retries=_int("QUEUE_MAX_RETRIES", 3),
        sigterm_grace_seconds=_int("SIGTERM_GRACE_SECONDS", 2),
        metrics_port=_int("METRICS_PORT", 9100),
        log_level=_str("LOG_LEVEL", "INFO"),
        otlp_endpoint=_str("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
    )
