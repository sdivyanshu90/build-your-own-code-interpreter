"""Shared pytest fixtures and Docker test doubles.

The ``FakeDocker`` harness lets us unit-test the full ``SandboxExecutor`` logic — command
construction, streaming, timeout/cancel ladder, OOM detection, metrics, and cleanup — without a
real daemon. Integration fixtures (``real_executor``, ``redis_client``) connect to actual
services and are skipped automatically when those are unavailable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator

import pytest

# Make the `worker` package importable when running from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.config import WorkerConfig
from worker.sandbox.executor import SandboxExecutor
from worker.sandbox.types import ExecutionRequest


# ── Fake Docker harness ──────────────────────────────────────────────────────────────────────
def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class _FakeStdin:
    """Captures bytes written to the sandbox program's stdin."""

    def __init__(self) -> None:
        self.received = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.received += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """A stand-in for ``asyncio.subprocess.Process`` driven by the test config."""

    def __init__(
        self, stdout: bytes, stderr: bytes, returncode: int, mode: str, delay: float
    ) -> None:
        self.stdout = _make_reader(stdout)
        self.stderr = _make_reader(stderr)
        self.stdin = _FakeStdin()
        self._final = returncode
        self._returncode: int | None = None
        self._mode = mode  # 'normal' | 'hang'
        self._delay = delay
        self._killed = asyncio.Event()

    def force_exit(self, code: int) -> None:
        """Simulate the container being SIGKILL'd (used by the fake `docker kill`)."""
        self._final = code
        self._killed.set()

    async def wait(self) -> int:
        if self._returncode is not None:
            return self._returncode
        if self._mode == "hang":
            await self._killed.wait()
        else:
            await asyncio.sleep(self._delay)
        self._returncode = self._final
        return self._returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode


class FakeDocker:
    """An in-memory Docker daemon double for the executor's spawn/simple seams."""

    def __init__(self) -> None:
        self.daemon_up = True
        self.image_present = True
        self.repo_digests = ["sandbox-runtime-python@sha256:" + "a" * 64]
        self.stdout = b""
        self.stderr = b""
        self.returncode = 0
        self.mode = "normal"  # 'normal' | 'hang'
        self.delay = 0.0
        self.run_calls: list[list[str]] = []
        self.removed: list[str] = []
        self.killed: list[tuple[str, str]] = []
        self.containers: dict[str, FakeProcess] = {}

    @property
    def last_run(self) -> list[str]:
        assert self.run_calls, "no docker run was issued"
        return self.run_calls[-1]

    async def spawn(self, argv: list[str]) -> FakeProcess:
        self.run_calls.append(argv)
        name = argv[argv.index("--name") + 1]
        proc = FakeProcess(self.stdout, self.stderr, self.returncode, self.mode, self.delay)
        self.containers[name] = proc
        return proc

    async def simple(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        sub = argv[1]
        if sub == "image":  # image inspect (verification)
            if not self.daemon_up:
                return (
                    1,
                    b"",
                    b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
                )
            if not self.image_present:
                return 1, b"", b"Error: No such image: sandbox-runtime-python:latest"
            import json

            return 0, json.dumps(self.repo_digests).encode(), b""
        if sub == "kill":
            signal = next((a.split("=")[1] for a in argv if a.startswith("--signal")), "TERM")
            name = argv[-1]
            self.killed.append((name, signal))
            proc = self.containers.get(name)
            if proc is not None and signal in ("KILL", "9", "SIGKILL"):
                proc.force_exit(137)
            return 0, b"", b""
        if sub == "inspect":  # container id resolution
            return 0, b"fakecontainerid0123456789\n", b""
        if sub == "rm":
            self.removed.append(argv[-1])
            return 0, b"", b""
        return 0, b"", b""

    def configure(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        mode: str = "normal",
        delay: float = 0.0,
    ) -> None:
        """Set what the next spawned container will produce/do."""
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.mode, self.delay = mode, delay


# ── Fixtures ─────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def workdir_base() -> Iterator[str]:
    """A throwaway base directory for per-job code dirs; asserted empty after cleanup."""
    path = tempfile.mkdtemp(prefix="sandbox-tests-")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def worker_config(workdir_base: str) -> WorkerConfig:
    """A WorkerConfig pointed at the throwaway workdir, with no host-path translation."""
    return WorkerConfig(
        redis_url="redis://localhost:6379/15",
        minio_endpoint="localhost:9000",
        minio_access_key="test",
        minio_secret_key="testsecret",
        minio_bucket="test-bucket",
        minio_secure=False,
        worker_concurrency=2,
        worker_id="test-worker",
        docker_path="docker",
        local_workdir=workdir_base,
        host_workdir="",
        block_ms=100,
        read_count=10,
        claim_min_idle_ms=60000,
        max_retries=3,
        sigterm_grace_seconds=0,
        metrics_port=0,
        log_level="INFO",
        otlp_endpoint="",
    )


@pytest.fixture
def fake_docker() -> FakeDocker:
    """A fresh FakeDocker harness."""
    return FakeDocker()


@pytest.fixture
def make_executor(worker_config: WorkerConfig, fake_docker: FakeDocker):
    """Factory that builds a SandboxExecutor wired to the fake Docker harness."""

    def _build(*, memory_value: int = 0, cpu_value: int = 0, clock=None) -> SandboxExecutor:
        async def mem_reader(_container: str) -> int:
            return memory_value

        async def cpu_reader(_container: str) -> int:
            return cpu_value

        kwargs = {} if clock is None else {"clock": clock}
        return SandboxExecutor(
            worker_config,
            spawn=fake_docker.spawn,
            simple=fake_docker.simple,
            memory_reader=mem_reader,
            cpu_reader=cpu_reader,
            **kwargs,
        )

    return _build


@pytest.fixture
def py_request() -> ExecutionRequest:
    """A simple valid Python request."""
    return ExecutionRequest(language="python", code="print('hello')", timeout_seconds=5)


# ── Integration fixtures (real services) ─────────────────────────────────────────────────────
def _docker_available() -> bool:
    import subprocess

    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """Session-scoped flag: is a real Docker daemon reachable?"""
    return _docker_available()


@pytest.fixture
def real_executor(worker_config: WorkerConfig, docker_available: bool) -> SandboxExecutor:
    """A SandboxExecutor backed by the real Docker daemon (integration tests)."""
    if not docker_available:
        pytest.skip("real Docker daemon not available")
    return SandboxExecutor(worker_config)


def _image_present(language: str) -> bool:
    import subprocess

    image = f"sandbox-runtime-{language}:{os.environ.get('SANDBOX_IMAGE_TAG', 'latest')}"
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except Exception:
        return False


@pytest.fixture(scope="session")
def available_languages(docker_available: bool) -> set[str]:
    """Set of languages whose runtime images are built and present locally."""
    if not docker_available:
        return set()
    langs = ["python", "javascript", "typescript", "java", "go", "ruby", "rust", "bash"]
    return {lang for lang in langs if _image_present(lang)}


@pytest.fixture
def require_language(available_languages: set[str]):
    """Returns a guard that skips a test when its runtime image is unavailable."""

    def _require(language: str) -> None:
        if language not in available_languages:
            pytest.skip(f"runtime image for {language} not built (run `make build-runtimes`)")

    return _require


@pytest.fixture
async def redis_client():
    """A real Redis client on test DB 15, flushed before and after. Skips if Redis is down."""
    import redis.asyncio as aioredis

    url = os.environ.get("REDIS_URL", "redis://localhost:6379/15")
    client = aioredis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis not available")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
