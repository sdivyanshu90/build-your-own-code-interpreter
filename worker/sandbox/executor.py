"""The core sandbox executor.

Runs untrusted code inside a single-use, maximally-restricted Docker container and returns a
fully-populated :class:`ExecutionResult`. Every container is launched with the full hardening
flag set (see :meth:`SandboxExecutor._build_command`): no network, read-only rootfs, dropped
capabilities, ``no-new-privileges``, a per-runtime seccomp profile, cgroup memory/CPU/PID limits,
ulimits, a non-root user, and a wall-clock timeout enforced with a SIGTERM→SIGKILL ladder.

The Docker interactions go through small injectable seams (``spawn`` / ``simple`` / readers) so the
executor is unit-testable with fakes, while the production path drives the real ``docker`` CLI via
``asyncio`` subprocesses with no shell interpolation (argv arrays only).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable

from worker.config import WorkerConfig
from worker.sandbox import constants
from worker.sandbox.errors import (
    DockerUnavailableError,
    ImageIntegrityError,
    ImageNotFoundError,
    SandboxError,
)
from worker.sandbox.image_registry import RuntimeConfig, get_runtime
from worker.sandbox.types import ExecutionRequest, ExecutionResult

# Type aliases for the injectable seams.
SpawnFn = Callable[[list[str]], Awaitable["asyncio.subprocess.Process"]]
SimpleFn = Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]]
ReaderFn = Callable[[str], Awaitable[int]]
OutputCallback = Callable[[str, str], Awaitable[None]]

# Exit status of a container that was SIGKILL'd (128 + 9). When we did not initiate the kill,
# this means the cgroup OOM killer fired.
_SIGKILL_EXIT = 137

# How often the resource sampler reads cgroup files, in seconds.
_SAMPLE_INTERVAL = 0.05

# Chunk size for streaming reads.
_READ_CHUNK = 65536

# Names of environment variables user code may never override (defence in depth with the API).
_FORBIDDEN_ENV = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "HOME",
        "TMPDIR",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "BASH_ENV",
        "ENV",
        "IFS",
        "RUBYOPT",
        "GEM_PATH",
        "CLASSPATH",
        "JAVA_TOOL_OPTIONS",
        "SANDBOX",
    }
)


class _OutputCapture:
    """Accumulates stdout/stderr under a shared combined byte cap, flagging truncation."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._used = 0
        self.truncated = False

    def append(self, kind: str, text: str) -> str:
        """Append text to the given stream; return the portion actually kept (post-cap)."""
        if self._used >= self.max_bytes:
            self.truncated = True
            return ""
        encoded = text.encode("utf-8")
        room = self.max_bytes - self._used
        if len(encoded) > room:
            text = encoded[:room].decode("utf-8", "ignore")
            self._used = self.max_bytes
            self.truncated = True
        else:
            self._used += len(encoded)
        (self._stdout if kind == "stdout" else self._stderr).append(text)
        return text

    @property
    def stdout(self) -> str:
        return "".join(self._stdout)

    @property
    def stderr(self) -> str:
        return "".join(self._stderr)


class _Sample:
    """Mutable holder the resource sampler writes peak memory / cpu usage into."""

    def __init__(self) -> None:
        self.peak_bytes = 0
        self.cpu_usec = 0


class SandboxExecutor:
    """Executes a single request in an isolated container and returns the result."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        spawn: SpawnFn | None = None,
        simple: SimpleFn | None = None,
        memory_reader: ReaderFn | None = None,
        cpu_reader: ReaderFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._spawn: SpawnFn = spawn or self._default_spawn
        self._simple: SimpleFn = simple or self._default_simple
        self._memory_reader: ReaderFn = memory_reader or self._default_memory_reader
        self._cpu_reader: ReaderFn = cpu_reader or self._default_cpu_reader
        self._clock = clock
        self._cid_cache: dict[str, str] = {}

    # ── public API ──────────────────────────────────────────────────────────────────────
    async def execute(
        self,
        request: ExecutionRequest,
        job_id: str,
        *,
        on_output: OutputCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> ExecutionResult:
        """Run the request in a sandbox and return its result. Always cleans up."""
        runtime = get_runtime(request.language)  # raises UnknownLanguageError
        await self._verify_image(runtime)
        workdir = self._prepare_workdir(runtime, request, job_id)
        container = f"sandbox-{job_id}"
        try:
            return await self._run(
                runtime, request, job_id, workdir, container, on_output, cancel_event
            )
        finally:
            await self._cleanup(container, workdir)

    # ── image verification ──────────────────────────────────────────────────────────────
    async def _verify_image(self, runtime: RuntimeConfig) -> None:
        """Ensure the image is present and (if pinned) its digest matches."""
        rc, out, err = await self._simple(
            [
                self.config.docker_path,
                "image",
                "inspect",
                runtime.image,
                "--format",
                "{{json .RepoDigests}}",
            ]
        )
        if rc != 0:
            text = err.decode("utf-8", "replace").lower()
            if (
                "cannot connect to the docker daemon" in text
                or "is the docker daemon running" in text
            ):
                raise DockerUnavailableError(err.decode("utf-8", "replace").strip())
            raise ImageNotFoundError(runtime.image)
        if runtime.image_digest:
            digests = out.decode("utf-8", "replace")
            if runtime.image_digest not in digests:
                raise ImageIntegrityError(runtime.image, runtime.image_digest, digests.strip())

    # ── workdir preparation ─────────────────────────────────────────────────────────────
    def _prepare_workdir(
        self, runtime: RuntimeConfig, request: ExecutionRequest, job_id: str
    ) -> str:
        """Create a per-job temp dir and write the code + input files (no shell interpolation)."""
        os.makedirs(self.config.local_workdir, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix=f"{job_id}-", dir=self.config.local_workdir)
        os.chmod(workdir, 0o755)
        self._write_file(workdir, runtime.source_filename, request.code)
        for input_file in request.files:
            safe = _safe_basename(input_file.name)
            if safe is None:
                raise SandboxError(
                    f"unsafe input filename: {input_file.name!r}", code="unsafe_filename"
                )
            self._write_file(workdir, safe, input_file.content)
        return workdir

    @staticmethod
    def _write_file(workdir: str, name: str, content: str) -> None:
        """Write a file inside ``workdir``, refusing any path that escapes it."""
        path = os.path.join(workdir, name)
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(workdir) + os.sep):
            raise SandboxError(f"path escapes sandbox dir: {name!r}", code="path_escape")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(real, 0o644)

    # ── command construction ────────────────────────────────────────────────────────────
    def _build_command(
        self, runtime: RuntimeConfig, request: ExecutionRequest, workdir: str, container: str
    ) -> list[str]:
        """Build the fully-hardened ``docker run`` argv. The single most security-sensitive step."""
        host_src = self.config.host_path_for(workdir)
        source_in = f"/sandbox/{runtime.source_filename}"
        work_in = "/build" if runtime.needs_exec_build else "/tmp"
        cmd: list[str] = [
            self.config.docker_path,
            "run",
            "--rm",  # auto-remove on exit
            "-i",  # attach stdin
            "--name",
            container,
            "--read-only",  # read-only root filesystem
            "--cap-drop",
            "ALL",  # drop every Linux capability
            "--security-opt",
            "no-new-privileges",  # block setuid privilege escalation
            "--security-opt",
            f"seccomp={runtime.seccomp_profile_path()}",
            "--user",
            "nobody",  # run as non-root
            "--hostname",
            "sandbox",
            "--label",
            "sandbox-managed=1",  # tag for the GC reaper
            "--stop-timeout",
            "2",  # fast SIGKILL after SIGTERM
        ]
        cmd += runtime.limits.to_docker_flags()  # memory, swap-off, cpus, pids, tmpfs, ulimits, net
        if runtime.needs_exec_build:
            # Compiled languages need a writable+EXECUTABLE build dir for their output binaries.
            # Docker's --tmpfs defaults to noexec, so `exec` must be set explicitly; /tmp stays
            # noexec. mode=1777 ensures the non-root `nobody` user can write to it.
            cmd += [
                "--tmpfs",
                f"/build:rw,exec,nosuid,nodev,size={runtime.limits.disk_mb}m,mode=1777",
            ]
        cmd += [
            "--workdir",
            work_in,
            "-e",
            "SANDBOX=1",
            "-e",
            f"HOME={work_in}",
            "-e",
            "TMPDIR=/tmp",
        ]
        for key, value in request.env_vars.items():
            if key.upper() in _FORBIDDEN_ENV or "=" in key or not key:
                continue
            cmd += ["-e", f"{key}={value}"]
        cmd += ["-v", f"{host_src}:/sandbox:ro", runtime.image]
        cmd += runtime.argv_for(source_in)
        return cmd

    # ── execution ───────────────────────────────────────────────────────────────────────
    async def _run(
        self,
        runtime: RuntimeConfig,
        request: ExecutionRequest,
        job_id: str,
        workdir: str,
        container: str,
        on_output: OutputCallback | None,
        cancel_event: asyncio.Event | None,
    ) -> ExecutionResult:
        """Spawn the container, stream output, enforce the timeout, and assemble the result."""
        timeout = runtime.limits.clamp_timeout(request.timeout_seconds)
        capture = _OutputCapture(runtime.limits.output_max_bytes)
        sample = _Sample()
        start = self._clock()

        proc = await self._spawn(self._build_command(runtime, request, workdir, container))
        stdin_task = asyncio.create_task(self._feed_stdin(proc, request.stdin.encode("utf-8")))
        pump = asyncio.gather(
            self._pump(proc.stdout, "stdout", capture, on_output),
            self._pump(proc.stderr, "stderr", capture, on_output),
        )
        sampler = asyncio.create_task(self._sample(container, sample))

        outcome = await self._await_completion(proc, container, timeout, cancel_event)

        await pump
        await self._quiet(stdin_task)
        sampler.cancel()
        await self._quiet(sampler)

        wall_ms = int((self._clock() - start) * 1000)
        return self._build_result(job_id, proc.returncode, outcome, capture, sample, wall_ms)

    async def _await_completion(
        self,
        proc: asyncio.subprocess.Process,
        container: str,
        timeout: int,
        cancel_event: asyncio.Event | None,
    ) -> str:
        """Wait for the process, the timeout, or a cancellation; return the outcome label."""
        wait_task = asyncio.create_task(proc.wait())
        waiters: set[asyncio.Task[object]] = {wait_task}
        cancel_task: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            waiters.add(cancel_task)

        done, pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            await self._quiet(task)

        if wait_task in done:
            return "completed"
        await self._terminate(proc, container)
        if cancel_task is not None and cancel_task in done:
            return "killed"
        return "timeout"

    async def _terminate(self, proc: asyncio.subprocess.Process, container: str) -> None:
        """SIGTERM the container, grace period, then SIGKILL."""
        await self._simple([self.config.docker_path, "kill", "--signal=TERM", container])
        try:
            await asyncio.wait_for(proc.wait(), self.config.sigterm_grace_seconds)
        except asyncio.TimeoutError:
            await self._simple([self.config.docker_path, "kill", "--signal=KILL", container])
            await proc.wait()

    @staticmethod
    async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
        """Write stdin to the program, then close it. Tolerates a program that never reads."""
        if proc.stdin is None:
            return
        try:
            if data:
                proc.stdin.write(data)
                await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass

    async def _pump(
        self,
        stream: asyncio.StreamReader | None,
        kind: str,
        capture: _OutputCapture,
        on_output: OutputCallback | None,
    ) -> None:
        """Read a stream to EOF, capturing under the cap and forwarding chunks to ``on_output``."""
        if stream is None:
            return
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                break
            kept = capture.append(kind, chunk.decode("utf-8", "replace"))
            if on_output is not None and kept:
                await on_output(kind, kept)

    def _build_result(
        self,
        job_id: str,
        returncode: int | None,
        outcome: str,
        capture: _OutputCapture,
        sample: _Sample,
        wall_ms: int,
    ) -> ExecutionResult:
        """Map the raw outcome into a typed :class:`ExecutionResult`."""
        oom = False
        if outcome == "completed":
            if returncode == _SIGKILL_EXIT:
                status, oom = constants.STATUS_FAILED, True
            else:
                status = constants.STATUS_COMPLETED
            exit_code = returncode
        elif outcome == "timeout":
            status, exit_code = constants.STATUS_TIMEOUT, None
        else:
            status, exit_code = constants.STATUS_KILLED, None
        return ExecutionResult(
            job_id=job_id,
            status=status,
            stdout=capture.stdout,
            stderr=capture.stderr,
            exit_code=exit_code,
            wall_time_ms=wall_ms,
            cpu_time_ms=sample.cpu_usec // 1000,
            memory_bytes=sample.peak_bytes,
            oom_killed=oom,
            timed_out=(outcome == "timeout"),
            truncated=capture.truncated,
        )

    # ── resource sampling ───────────────────────────────────────────────────────────────
    async def _sample(self, container: str, sample: _Sample) -> None:
        """Poll cgroup memory/cpu while the container runs, tracking the peak."""
        try:
            while True:
                mem = await self._memory_reader(container)
                if mem > sample.peak_bytes:
                    sample.peak_bytes = mem
                cpu = await self._cpu_reader(container)
                if cpu:
                    sample.cpu_usec = cpu
                await asyncio.sleep(_SAMPLE_INTERVAL)
        except asyncio.CancelledError:
            # Final read to catch the peak (and any CPU usage) right before teardown.
            try:
                mem = await self._memory_reader(container)
                if mem > sample.peak_bytes:
                    sample.peak_bytes = mem
                cpu = await self._cpu_reader(container)
                if cpu:
                    sample.cpu_usec = cpu
            except Exception:  # pragma: no cover - best effort
                pass
            raise

    # ── cleanup ─────────────────────────────────────────────────────────────────────────
    async def _cleanup(self, container: str, workdir: str) -> None:
        """Force-remove the container (belt and braces over --rm) and delete the temp dir."""
        self._cid_cache.pop(container, None)
        # Container may already be gone (--rm); suppress the resulting error.
        with contextlib.suppress(Exception):  # pragma: no cover - container may already be gone
            await self._simple([self.config.docker_path, "rm", "-f", container])
        shutil.rmtree(workdir, ignore_errors=True)

    # ── default (real) seam implementations ─────────────────────────────────────────────
    async def _default_spawn(
        self, argv: list[str]
    ) -> asyncio.subprocess.Process:  # pragma: no cover - real subprocess IO
        """Spawn the docker run subprocess with piped stdio."""
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @staticmethod
    async def _default_simple(
        argv: list[str],
    ) -> tuple[int, bytes, bytes]:  # pragma: no cover - real subprocess IO
        """Run a one-shot docker command, returning (rc, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1, out, err

    async def _resolve_cid(self, container: str) -> str:
        """Resolve and cache a container's full id from its name (for cgroup paths)."""
        if container in self._cid_cache:
            return self._cid_cache[container]
        rc, out, _ = await self._simple(
            [self.config.docker_path, "inspect", "-f", "{{.Id}}", container]
        )
        cid = out.decode("utf-8", "replace").strip() if rc == 0 else ""
        if cid:
            self._cid_cache[container] = cid
        return cid

    async def _default_memory_reader(self, container: str) -> int:
        """Read peak/current memory for a container from the host cgroupfs (best effort)."""
        cid = await self._resolve_cid(container)
        if not cid:
            return 0
        for path in (
            f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.peak",
            f"/sys/fs/cgroup/docker/{cid}/memory.peak",
            f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current",
            f"/sys/fs/cgroup/docker/{cid}/memory.current",
            f"/sys/fs/cgroup/memory/docker/{cid}/memory.max_usage_in_bytes",
        ):
            value = _read_int_file(path)
            if value is not None:
                return value
        return 0

    async def _default_cpu_reader(self, container: str) -> int:
        """Read cumulative CPU usage (microseconds) for a container from cgroupfs (best effort)."""
        cid = await self._resolve_cid(container)
        if not cid:
            return 0
        for path in (
            f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat",
            f"/sys/fs/cgroup/docker/{cid}/cpu.stat",
        ):
            usec = _read_usage_usec(path)
            if usec is not None:
                return usec
        nanos = _read_int_file(f"/sys/fs/cgroup/cpu,cpuacct/docker/{cid}/cpuacct.usage")
        return nanos // 1000 if nanos is not None else 0

    @staticmethod
    async def _quiet(task: asyncio.Task[object] | asyncio.Future[object]) -> None:
        """Await a task, swallowing cancellation and any error (used for cleanup awaits)."""
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _safe_basename(name: str) -> str | None:
    """Return a safe basename confined to the sandbox dir, or None if it cannot be made safe."""
    if "\0" in name:
        return None
    base = os.path.basename(name.replace("\\", "/"))
    if base in ("", ".", ".."):
        return None
    if not all(c.isalnum() or c in "._-" for c in base):
        return None
    if len(base) > 255:
        return None
    return base


def _read_int_file(path: str) -> int | None:
    """Read a file containing a single integer; return None on any error."""
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _read_usage_usec(path: str) -> int | None:
    """Parse ``usage_usec`` from a cgroup v2 ``cpu.stat`` file; return None on error."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None
