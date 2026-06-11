"""Unit tests for the container reaper and its status parser."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from worker.config import WorkerConfig
from worker.sandbox.cleanup import (
    ORPHAN_AGE_SECONDS,
    ContainerReaper,
    _parse_up_age,
    should_reap,
)


class TestShouldReap:
    def test_exited_is_reaped(self):
        assert should_reap("Exited (0) 3 seconds ago") is True
        assert should_reap("Exited (137) 1 minute ago") is True

    def test_dead_and_created_are_reaped(self):
        assert should_reap("Dead") is True
        assert should_reap("Created") is True

    def test_young_running_is_not_reaped(self):
        assert should_reap("Up 3 seconds") is False
        assert should_reap("Up 2 minutes") is False
        assert should_reap("Up About a minute") is False

    def test_old_running_is_reaped(self):
        assert should_reap("Up 10 minutes") is True
        assert should_reap("Up 2 hours") is True
        assert should_reap("Up About an hour") is True

    def test_unknown_running_is_left_alone(self):
        assert should_reap("Up since forever") is False


class TestParseUpAge:
    def test_seconds(self):
        assert _parse_up_age("Up 45 seconds") == 45

    def test_minutes(self):
        assert _parse_up_age("Up 6 minutes") == 360

    def test_about_a_minute(self):
        assert _parse_up_age("Up About a minute") == 60

    def test_hours(self):
        assert _parse_up_age("Up 2 hours") == 7200

    def test_less_than_a_second(self):
        assert _parse_up_age("Up Less than a second") == 0

    def test_unparseable(self):
        assert _parse_up_age("Up forever and ever") is None


@pytest.fixture
def reaper_config():
    workdir = tempfile.mkdtemp(prefix="reaper-tests-")
    cfg = WorkerConfig(
        redis_url="redis://localhost:6379/15",
        minio_endpoint="x",
        minio_access_key="x",
        minio_secret_key="x",
        minio_bucket="x",
        minio_secure=False,
        worker_concurrency=1,
        worker_id="reaper",
        docker_path="docker",
        local_workdir=workdir,
        host_workdir="",
        block_ms=100,
        read_count=1,
        claim_min_idle_ms=1000,
        max_retries=3,
        sigterm_grace_seconds=0,
        metrics_port=0,
        log_level="INFO",
        otlp_endpoint="",
    )
    yield cfg
    shutil.rmtree(workdir, ignore_errors=True)


class TestContainerReaper:
    async def test_reaps_exited_and_old_containers_only(self, reaper_config):
        ps_output = (
            b"id_exited\tExited (0) 5 seconds ago\n"
            b"id_young\tUp 4 seconds\n"
            b"id_orphan\tUp 9 minutes\n"
        )
        removed: list[str] = []

        async def fake_simple(argv):
            if argv[1:3] == ["ps", "-a"]:
                return 0, ps_output, b""
            if argv[1] == "rm":
                removed.append(argv[-1])
                return 0, b"", b""
            return 0, b"", b""

        reaper = ContainerReaper(reaper_config, simple=fake_simple)
        count = await reaper.reap_once()
        assert count == 2
        assert "id_exited" in removed and "id_orphan" in removed
        assert "id_young" not in removed  # active job must be left running

    async def test_reaps_stale_workdirs(self, reaper_config):
        old_dir = os.path.join(reaper_config.local_workdir, "old-job")
        new_dir = os.path.join(reaper_config.local_workdir, "new-job")
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        # Backdate the old dir well beyond the orphan threshold.
        old_time = 1000.0
        os.utime(old_dir, (old_time, old_time))

        async def fake_simple(argv):
            return 0, b"", b""

        reaper = ContainerReaper(
            reaper_config, simple=fake_simple, clock=lambda: old_time + ORPHAN_AGE_SECONDS + 100
        )
        await reaper.reap_once()
        assert not os.path.exists(old_dir)
        assert os.path.exists(new_dir)  # recent dir (active) is preserved

    async def test_ps_failure_is_handled(self, reaper_config):
        async def fake_simple(argv):
            return 1, b"", b"daemon down"

        reaper = ContainerReaper(reaper_config, simple=fake_simple)
        assert await reaper.reap_once() == 0
