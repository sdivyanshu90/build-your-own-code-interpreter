"""Unit tests for constants, errors, the seccomp CLI, the registry, and data types."""

from __future__ import annotations

import asyncio
import json

import pytest

from worker.sandbox import constants, seccomp
from worker.sandbox.cleanup import ContainerReaper
from worker.sandbox.errors import (
    DockerUnavailableError,
    ImageIntegrityError,
    ImageNotFoundError,
    SandboxError,
    UnknownLanguageError,
)
from worker.sandbox.image_registry import (
    get_runtime,
    list_runtimes,
    reload_registry,
)
from worker.sandbox.types import (
    ExecutionRequest,
    ExecutionResult,
    InputFile,
    OutputFile,
)


class TestConstants:
    def test_key_helpers(self):
        assert constants.job_record_key("abc") == "sandbox:job:abc"
        assert constants.result_key("abc") == "sandbox:result:abc"
        assert constants.stream_channel("abc") == "sandbox:stream:abc"
        assert constants.cancel_key("abc") == "sandbox:cancel:abc"

    def test_terminal_statuses(self):
        assert constants.STATUS_COMPLETED in constants.TERMINAL_STATUSES
        assert constants.STATUS_PENDING not in constants.TERMINAL_STATUSES


class TestErrors:
    def test_unknown_language(self):
        err = UnknownLanguageError("cobol")
        assert err.code == "unknown_language" and err.language == "cobol"

    def test_image_not_found(self):
        err = ImageNotFoundError("img:tag")
        assert err.code == "image_not_found" and "img:tag" in str(err)

    def test_image_integrity(self):
        err = ImageIntegrityError("img", "sha256:a", "sha256:b")
        assert err.code == "image_integrity"
        assert err.expected == "sha256:a" and err.actual == "sha256:b"

    def test_docker_unavailable(self):
        err = DockerUnavailableError("connection refused")
        assert err.code == "docker_unavailable"

    def test_base_sandbox_error(self):
        err = SandboxError("oops")
        assert err.code == "sandbox_error" and str(err) == "oops"


class TestSeccompCli:
    def test_main_prints_valid_profile(self, capsys):
        rc = seccomp._main(["seccomp", "python"])
        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out)["defaultAction"] == "SCMP_ACT_ERRNO"

    def test_main_with_network_flag(self, capsys):
        rc = seccomp._main(["seccomp", "python", "--network"])
        assert rc == 0
        allowed = set(json.loads(capsys.readouterr().out)["syscalls"][0]["names"])
        assert "socket" in allowed

    def test_main_without_args_errors(self):
        assert seccomp._main(["seccomp"]) == 2

    def test_profile_path_writes_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECCOMP_CACHE_DIR", str(tmp_path))
        seccomp.profile_path.cache_clear()
        path = seccomp.profile_path("ruby")
        assert path.endswith("ruby.json")
        assert json.load(open(path))["defaultAction"] == "SCMP_ACT_ERRNO"


class TestRegistry:
    def test_list_runtimes_sorted(self):
        runtimes = list_runtimes()
        ids = [r.id for r in runtimes]
        assert ids == sorted(ids)
        assert len(ids) == 8

    def test_argv_for_appends_source(self):
        runtime = get_runtime("python")
        argv = runtime.argv_for("/sandbox/main.py")
        assert argv[-1] == "/sandbox/main.py"
        assert argv[0] == "python3"

    def test_unknown_language_raises(self):
        with pytest.raises(UnknownLanguageError):
            get_runtime("fortran")

    def test_seccomp_profile_path_resolves(self):
        path = get_runtime("python").seccomp_profile_path()
        assert path.endswith("python.json")

    def test_reload_registry_picks_up_env(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_IMAGE_TAG", "v9")
        reload_registry()
        try:
            assert get_runtime("python").image.endswith(":v9")
        finally:
            monkeypatch.delenv("SANDBOX_IMAGE_TAG", raising=False)
            reload_registry()

    def test_compiled_languages_need_exec_build(self):
        assert get_runtime("go").needs_exec_build is True
        assert get_runtime("rust").needs_exec_build is True
        assert get_runtime("python").needs_exec_build is False


class TestTypes:
    def test_request_from_dict_full(self):
        req = ExecutionRequest.from_dict(
            {
                "language": "python",
                "code": "x",
                "stdin": "in",
                "timeout_seconds": 7,
                "env_vars": {"A": "1"},
                "files": [{"name": "f.txt", "content": "data"}],
            }
        )
        assert req.language == "python" and req.timeout_seconds == 7
        assert req.files == [InputFile(name="f.txt", content="data")]
        assert req.env_vars == {"A": "1"}

    def test_request_from_dict_minimal(self):
        req = ExecutionRequest.from_dict({"language": "bash", "code": "echo"})
        assert req.stdin == "" and req.timeout_seconds == 10 and req.files == []

    def test_result_to_dict_roundtrip(self):
        result = ExecutionResult(
            job_id="j1",
            status="COMPLETED",
            stdout="o",
            stderr="e",
            exit_code=0,
            wall_time_ms=5,
            cpu_time_ms=1,
            memory_bytes=99,
            files=[OutputFile("a", 3, "u")],
        )
        d = result.to_dict()
        assert d["job_id"] == "j1" and d["status"] == "COMPLETED"
        assert d["files"] == [{"name": "a", "size_bytes": 3, "url": "u"}]
        assert d["oom_killed"] is False and d["timed_out"] is False


class TestReaperLoop:
    async def test_run_forever_stops(self):
        from worker.config import WorkerConfig

        cfg = WorkerConfig(
            redis_url="redis://localhost:6379/15",
            minio_endpoint="x",
            minio_access_key="x",
            minio_secret_key="x",
            minio_bucket="x",
            minio_secure=False,
            worker_concurrency=1,
            worker_id="r",
            docker_path="docker",
            local_workdir="/tmp/nonexistent-reaper-xyz",
            host_workdir="",
            block_ms=1,
            read_count=1,
            claim_min_idle_ms=1,
            max_retries=1,
            sigterm_grace_seconds=0,
            metrics_port=0,
            log_level="INFO",
            otlp_endpoint="",
        )
        cycles = 0

        async def fake_simple(_argv):
            nonlocal cycles
            cycles += 1
            return 0, b"", b""

        reaper = ContainerReaper(cfg, simple=fake_simple, interval_seconds=0.02)
        task = asyncio.create_task(reaper.run_forever())
        await asyncio.sleep(0.01)
        reaper.stop()
        await asyncio.wait_for(task, timeout=2)
        assert cycles >= 1
