"""Unit tests for the executor's metric-collection seams and helpers."""

from __future__ import annotations

import os
import tempfile

import pytest

from worker.sandbox import executor as executor_module
from worker.sandbox.executor import (
    SandboxExecutor,
    _OutputCapture,
    _read_int_file,
    _read_usage_usec,
    _safe_basename,
)


@pytest.fixture
def default_executor(worker_config, fake_docker):
    """An executor that uses the real cgroup/cid reader seams but a fake docker simple()."""
    return SandboxExecutor(worker_config, spawn=fake_docker.spawn, simple=fake_docker.simple)


class TestCidResolution:
    async def test_resolve_cid_success_and_cache(self, default_executor, fake_docker):
        cid = await default_executor._resolve_cid("sandbox-x")
        assert cid == "fakecontainerid0123456789"
        # Second call is served from cache (no extra inspect needed for correctness).
        assert await default_executor._resolve_cid("sandbox-x") == cid

    async def test_resolve_cid_failure_returns_empty(self, worker_config):
        async def failing_simple(_argv):
            return 1, b"", b"no such container"

        ex = SandboxExecutor(worker_config, simple=failing_simple)
        assert await ex._resolve_cid("missing") == ""


class TestMemoryReader:
    async def test_reads_cgroup_value(self, default_executor, monkeypatch):
        monkeypatch.setattr(executor_module, "_read_int_file", lambda _p: 4096)
        assert await default_executor._default_memory_reader("sandbox-x") == 4096

    async def test_returns_zero_without_cid(self, worker_config):
        async def failing_simple(_argv):
            return 1, b"", b""

        ex = SandboxExecutor(worker_config, simple=failing_simple)
        assert await ex._default_memory_reader("missing") == 0

    async def test_returns_zero_when_no_cgroup_file(self, default_executor, monkeypatch):
        monkeypatch.setattr(executor_module, "_read_int_file", lambda _p: None)
        assert await default_executor._default_memory_reader("sandbox-x") == 0


class TestCpuReader:
    async def test_reads_cpu_stat(self, default_executor, monkeypatch):
        monkeypatch.setattr(executor_module, "_read_usage_usec", lambda _p: 7000)
        assert await default_executor._default_cpu_reader("sandbox-x") == 7000

    async def test_falls_back_to_v1_nanos(self, default_executor, monkeypatch):
        monkeypatch.setattr(executor_module, "_read_usage_usec", lambda _p: None)
        monkeypatch.setattr(executor_module, "_read_int_file", lambda _p: 9_000_000)  # nanos
        assert await default_executor._default_cpu_reader("sandbox-x") == 9000  # → microseconds

    async def test_returns_zero_without_cid(self, worker_config):
        async def failing_simple(_argv):
            return 1, b"", b""

        ex = SandboxExecutor(worker_config, simple=failing_simple)
        assert await ex._default_cpu_reader("missing") == 0


class TestFileReaders:
    def test_read_int_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("262144\n")
            path = handle.name
        try:
            assert _read_int_file(path) == 262144
        finally:
            os.unlink(path)

    def test_read_int_file_missing_returns_none(self):
        assert _read_int_file("/nonexistent/path/memory.peak") is None

    def test_read_int_file_garbage_returns_none(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("not-a-number")
            path = handle.name
        try:
            assert _read_int_file(path) is None
        finally:
            os.unlink(path)

    def test_read_usage_usec(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("usage_usec 123456\nuser_usec 1\nsystem_usec 2\n")
            path = handle.name
        try:
            assert _read_usage_usec(path) == 123456
        finally:
            os.unlink(path)

    def test_read_usage_usec_missing_returns_none(self):
        assert _read_usage_usec("/nonexistent/cpu.stat") is None

    def test_read_usage_usec_no_field_returns_none(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("user_usec 1\nsystem_usec 2\n")
            path = handle.name
        try:
            assert _read_usage_usec(path) is None
        finally:
            os.unlink(path)


class TestOutputCapture:
    def test_within_cap_keeps_everything(self):
        cap = _OutputCapture(100)
        assert cap.append("stdout", "hello") == "hello"
        assert cap.stdout == "hello"
        assert cap.truncated is False

    def test_truncates_at_cap(self):
        cap = _OutputCapture(4)
        kept = cap.append("stdout", "abcdefgh")
        assert kept == "abcd"
        assert cap.truncated is True

    def test_append_after_full_returns_empty(self):
        cap = _OutputCapture(4)
        cap.append("stdout", "abcd")
        assert cap.append("stderr", "more") == ""
        assert cap.truncated is True

    def test_stdout_and_stderr_are_separate(self):
        cap = _OutputCapture(100)
        cap.append("stdout", "OUT")
        cap.append("stderr", "ERR")
        assert cap.stdout == "OUT" and cap.stderr == "ERR"


class TestSafeBasename:
    def test_rejects_traversal(self):
        assert _safe_basename("../../etc/passwd") in (None, "passwd")
        assert _safe_basename("/etc/passwd") == "passwd"  # confined to basename

    def test_rejects_null_byte(self):
        assert _safe_basename("a\0b") is None

    def test_rejects_dotdot(self):
        assert _safe_basename("..") is None

    def test_rejects_weird_chars(self):
        assert _safe_basename("a;b") is None

    def test_accepts_normal(self):
        assert _safe_basename("data.csv") == "data.csv"
