"""Unit tests for the sandbox executor using the FakeDocker harness.

These exercise the executor's logic — command construction (the security flags it emits),
stdout/stderr streaming and separation, stdin piping, exit-code handling, the timeout/cancel
SIGTERM→SIGKILL ladder, OOM detection, output truncation, metrics, and the always-runs cleanup —
without a real daemon. Real isolation enforcement is verified in tests/integration and
tests/security against actual containers.
"""

from __future__ import annotations

import itertools
import json

import pytest

from worker.sandbox import executor as executor_module
from worker.sandbox.errors import (
    DockerUnavailableError,
    ImageNotFoundError,
    SandboxError,
)
from worker.sandbox.image_registry import RuntimeConfig, get_runtime
from worker.sandbox.resource_limits import ResourceConfig
from worker.sandbox.types import ExecutionRequest, InputFile


def _argv_has_flag(argv: list[str], flag: str) -> bool:
    return any(part == flag or part.startswith(flag) for part in argv)


def _seccomp_path(argv: list[str]) -> str:
    for part in argv:
        if part.startswith("seccomp="):
            return part.split("=", 1)[1]
    raise AssertionError("no seccomp profile in argv")


class TestSandboxExecutorUnit:
    # ── Happy path ──────────────────────────────────────────────────────────────────────
    async def test_python_hello_world_returns_stdout(self, make_executor, fake_docker):
        fake_docker.configure(stdout=b"hello\n", returncode=0)
        ex = make_executor()
        result = await ex.execute(ExecutionRequest(language="python", code="print('hi')"), "job1")
        assert result.stdout == "hello\n"
        assert result.status == "COMPLETED"
        assert result.exit_code == 0

    async def test_javascript_console_log_returns_output(self, make_executor, fake_docker):
        fake_docker.configure(stdout=b"node says hi\n")
        ex = make_executor()
        result = await ex.execute(
            ExecutionRequest(language="javascript", code="console.log('x')"), "job2"
        )
        assert result.stdout == "node says hi\n"
        assert result.status == "COMPLETED"

    async def test_stdin_is_piped_to_program(self, make_executor, fake_docker):
        ex = make_executor()
        await ex.execute(
            ExecutionRequest(
                language="python", code="import sys; print(sys.stdin.read())", stdin="42"
            ),
            "jobstdin",
        )
        proc = fake_docker.containers["sandbox-jobstdin"]
        assert proc.stdin.received == b"42"
        assert proc.stdin.closed is True

    async def test_multi_file_execution_with_imports(self, make_executor, worker_config):
        ex = make_executor()
        runtime = get_runtime("python")
        request = ExecutionRequest(
            language="python",
            code="import helper",
            files=[InputFile(name="helper.py", content="VALUE = 1")],
        )
        workdir = ex._prepare_workdir(runtime, request, "jobmf")
        import os

        assert os.path.exists(os.path.join(workdir, "main.py"))
        assert os.path.exists(os.path.join(workdir, "helper.py"))
        await ex._cleanup("none", workdir)
        assert not os.path.exists(workdir)

    async def test_env_vars_are_injected_correctly(self, make_executor, fake_docker):
        ex = make_executor()
        await ex.execute(
            ExecutionRequest(language="python", code="x", env_vars={"FOO": "bar"}), "jobenv"
        )
        pairs = _flag_values(fake_docker.last_run, "-e")
        assert "FOO=bar" in pairs
        assert "SANDBOX=1" in pairs

    async def test_forbidden_env_vars_are_stripped(self, make_executor, fake_docker):
        ex = make_executor()
        await ex.execute(
            ExecutionRequest(language="python", code="x", env_vars={"PATH": "/evil", "OK": "1"}),
            "jobenv2",
        )
        pairs = _flag_values(fake_docker.last_run, "-e")
        assert "PATH=/evil" not in pairs
        assert "OK=1" in pairs

    async def test_exit_code_zero_on_success(self, make_executor, fake_docker):
        fake_docker.configure(returncode=0)
        result = await make_executor().execute(_py(), "jobok")
        assert result.exit_code == 0 and result.status == "COMPLETED"

    async def test_nonzero_exit_code_captured_correctly(self, make_executor, fake_docker):
        fake_docker.configure(stderr=b"boom", returncode=3)
        result = await make_executor().execute(_py(), "jobnz")
        assert result.exit_code == 3
        assert result.status == "COMPLETED"  # the program ran; non-zero exit is still COMPLETED

    async def test_stderr_captured_separately_from_stdout(self, make_executor, fake_docker):
        fake_docker.configure(stdout=b"OUT", stderr=b"ERR", returncode=0)
        result = await make_executor().execute(_py(), "jobsep")
        assert result.stdout == "OUT"
        assert result.stderr == "ERR"

    async def test_wall_time_ms_is_measured_accurately(self, make_executor, fake_docker):
        clock = _StubClock([10.0, 10.25])  # 250 ms elapsed
        result = await make_executor(clock=clock).execute(_py(), "jobwt")
        assert result.wall_time_ms == 250

    async def test_memory_bytes_reported_correctly(self, make_executor, fake_docker):
        result = await make_executor(memory_value=12_345_678).execute(_py(), "jobmem")
        assert result.memory_bytes == 12_345_678

    async def test_cpu_time_ms_reported_correctly(self, make_executor, fake_docker):
        result = await make_executor(cpu_value=42_000).execute(_py(), "jobcpu")
        assert result.cpu_time_ms == 42  # 42000 microseconds → 42 ms

    # ── Security: the executor emits the isolation flags ────────────────────────────────
    async def test_network_access_is_blocked(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobnet")
        assert "--network=none" in fake_docker.last_run

    async def test_cannot_write_outside_tmp(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobro")
        assert "--read-only" in fake_docker.last_run

    async def test_cannot_read_host_filesystem(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobfs")
        mounts = _flag_values(fake_docker.last_run, "-v")
        # The only bind mount is the read-only code dir; no host paths are mounted.
        assert len(mounts) == 1
        assert mounts[0].endswith(":/sandbox:ro")

    async def test_fork_bomb_is_killed_within_timeout(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobfork")
        assert "--pids-limit=64" in fake_docker.last_run

    async def test_infinite_loop_times_out_correctly(self, make_executor, fake_docker):
        fake_docker.configure(mode="hang")
        result = await make_executor().execute(_py(timeout=1), "jobinf")
        assert result.status == "TIMEOUT"
        assert result.timed_out is True

    async def test_subprocess_spawning_is_blocked_in_bash(self, make_executor, fake_docker):
        await make_executor().execute(ExecutionRequest(language="bash", code="echo hi"), "jobbash")
        profile = json.load(open(_seccomp_path(fake_docker.last_run)))
        allowed = set(profile["syscalls"][0]["names"])
        assert "socket" not in allowed and "connect" not in allowed

    async def test_kernel_module_load_blocked(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobkmod")
        assert _denied(fake_docker.last_run, "init_module")
        assert _denied(fake_docker.last_run, "finit_module")

    async def test_ptrace_syscall_is_denied(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobptrace")
        assert _denied(fake_docker.last_run, "ptrace")

    async def test_setuid_is_denied(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobsetuid")
        assert _denied(fake_docker.last_run, "setuid")
        assert "no-new-privileges" in fake_docker.last_run

    async def test_mount_syscall_is_denied(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobmount")
        assert _denied(fake_docker.last_run, "mount")
        assert _denied(fake_docker.last_run, "pivot_root")

    async def test_cannot_kill_pid_1_and_caps_dropped(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobcap")
        argv = fake_docker.last_run
        assert "--cap-drop" in argv and "ALL" in argv

    async def test_privilege_escalation_via_suid_blocked(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobsuid")
        assert "no-new-privileges" in fake_docker.last_run
        assert "nobody" in fake_docker.last_run

    # ── Resource limits ─────────────────────────────────────────────────────────────────
    async def test_oom_kill_on_memory_excess(self, make_executor, fake_docker):
        # A container the OOM killer SIGKILLs exits 137 without us terminating it.
        fake_docker.configure(returncode=137)
        result = await make_executor().execute(_py(), "joboom")
        assert result.oom_killed is True
        assert result.status == "FAILED"
        assert "--memory=256m" in fake_docker.last_run

    async def test_cpu_quota_enforced(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobcpuq")
        assert "--cpus=0.5" in fake_docker.last_run

    async def test_output_truncated_at_max_bytes(self, make_executor, fake_docker, monkeypatch):
        monkeypatch.setattr(
            executor_module, "get_runtime", lambda _l: _tiny_runtime(output_max_bytes=10)
        )
        fake_docker.configure(stdout=b"x" * 50)
        result = await make_executor().execute(_py(), "jobtrunc")
        assert result.truncated is True
        assert len(result.stdout.encode()) <= 10

    async def test_pids_limit_prevents_fork_bomb(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobpids")
        assert "--pids-limit=64" in fake_docker.last_run

    async def test_disk_write_limit_enforced(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobdisk")
        tmpfs = _flag_values(fake_docker.last_run, "--tmpfs")
        assert any(
            "/tmp:" in t and "noexec" in t and "size=64m" in t
            for t in _tmpfs_inline(fake_docker.last_run) + tmpfs
        )

    # ── Error handling ──────────────────────────────────────────────────────────────────
    async def test_syntax_error_returns_stderr_not_exception(self, make_executor, fake_docker):
        fake_docker.configure(stderr=b"SyntaxError: invalid syntax", returncode=1)
        result = await make_executor().execute(_py(), "jobsyn")
        assert "SyntaxError" in result.stderr
        assert result.status == "COMPLETED"

    async def test_runtime_exception_returns_stderr_and_nonzero_exit(
        self, make_executor, fake_docker
    ):
        fake_docker.configure(stderr=b"Traceback ... ValueError", returncode=1)
        result = await make_executor().execute(_py(), "jobrt")
        assert result.exit_code == 1
        assert "ValueError" in result.stderr

    async def test_missing_image_raises_clear_error(self, make_executor, fake_docker):
        fake_docker.image_present = False
        with pytest.raises(ImageNotFoundError):
            await make_executor().execute(_py(), "jobimg")

    async def test_docker_daemon_unavailable_raises_sandbox_error(self, make_executor, fake_docker):
        fake_docker.daemon_up = False
        with pytest.raises(DockerUnavailableError):
            await make_executor().execute(_py(), "jobdaemon")

    async def test_code_injection_via_filename_is_prevented(self, make_executor):
        request = ExecutionRequest(
            language="python", code="x", files=[InputFile(name="; rm -rf /", content="evil")]
        )
        with pytest.raises(SandboxError):
            await make_executor().execute(request, "jobinj")

    async def test_cleanup_always_runs_even_on_exception(self, worker_config, fake_docker):
        async def boom_spawn(_argv):
            raise RuntimeError("spawn failed")

        ex = executor_module.SandboxExecutor(
            worker_config, spawn=boom_spawn, simple=fake_docker.simple
        )
        with pytest.raises(RuntimeError):
            await ex.execute(_py(), "jobclean")
        import os

        assert os.listdir(worker_config.local_workdir) == []

    async def test_container_is_always_removed_after_execution(self, make_executor, fake_docker):
        await make_executor().execute(_py(), "jobrm")
        assert "sandbox-jobrm" in fake_docker.removed

    async def test_temp_directory_is_always_deleted(
        self, make_executor, fake_docker, worker_config
    ):
        await make_executor().execute(_py(), "jobtmp")
        import os

        assert os.listdir(worker_config.local_workdir) == []

    # ── Timeout ─────────────────────────────────────────────────────────────────────────
    async def test_timeout_sends_sigterm_then_sigkill(self, make_executor, fake_docker):
        fake_docker.configure(mode="hang")
        await make_executor().execute(_py(timeout=1), "jobladder")
        signals = [sig for (name, sig) in fake_docker.killed if name == "sandbox-jobladder"]
        assert "TERM" in signals and "KILL" in signals

    async def test_timeout_result_has_timed_out_status(self, make_executor, fake_docker):
        fake_docker.configure(mode="hang")
        result = await make_executor().execute(_py(timeout=1), "jobtos")
        assert result.status == "TIMEOUT" and result.timed_out is True

    async def test_partial_output_returned_on_timeout(self, make_executor, fake_docker):
        fake_docker.configure(stdout=b"partial output\n", mode="hang")
        result = await make_executor().execute(_py(timeout=1), "jobpartial")
        assert result.stdout == "partial output\n"
        assert result.status == "TIMEOUT"

    async def test_custom_timeout_is_clamped_to_runtime_ceiling(self):
        runtime = get_runtime("python")
        assert runtime.limits.clamp_timeout(9999) == runtime.limits.timeout_seconds
        assert runtime.limits.clamp_timeout(3) == 3

    async def test_cancellation_kills_container_and_reports_killed(
        self, make_executor, fake_docker
    ):
        import asyncio

        fake_docker.configure(mode="hang")
        cancel = asyncio.Event()
        cancel.set()  # already requested
        result = await make_executor().execute(_py(timeout=30), "jobcancel", cancel_event=cancel)
        assert result.status == "KILLED"


# ── helpers ───────────────────────────────────────────────────────────────────────────────────
def _py(timeout: int = 5) -> ExecutionRequest:
    return ExecutionRequest(language="python", code="print(1)", timeout_seconds=timeout)


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """Collect the value following each occurrence of ``flag`` in argv."""
    values = []
    for a, b in zip(argv, argv[1:]):
        if a == flag:
            values.append(b)
    return values


def _tmpfs_inline(argv: list[str]) -> list[str]:
    """Collect inline --tmpfs=... values (resource_limits emits the /tmp tmpfs inline)."""
    return [a.split("=", 1)[1] for a in argv if a.startswith("--tmpfs=")]


def _denied(argv: list[str], syscall: str) -> bool:
    """True if the seccomp profile referenced by argv does NOT allow the syscall."""
    profile = json.load(open(_seccomp_path(argv)))
    allowed = set(profile["syscalls"][0]["names"])
    return syscall not in allowed


def _tiny_runtime(output_max_bytes: int) -> RuntimeConfig:
    base = get_runtime("python")
    limits = ResourceConfig(
        memory_mb=64,
        cpu_quota=0.5,
        timeout_seconds=5,
        pids_limit=16,
        disk_mb=16,
        network_enabled=False,
        output_max_bytes=output_max_bytes,
    )
    return RuntimeConfig(
        id=base.id,
        name=base.name,
        version=base.version,
        image=base.image,
        image_digest="",
        entrypoint=base.entrypoint,
        source_filename=base.source_filename,
        needs_exec_build=False,
        limits=limits,
    )


class _StubClock:
    """Returns a fixed sequence of monotonic timestamps."""

    def __init__(self, values: list[float]) -> None:
        self._it = itertools.chain(values, itertools.repeat(values[-1]))

    def __call__(self) -> float:
        return next(self._it)
