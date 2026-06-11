"""Unit tests for per-language resource configuration."""

from __future__ import annotations

from worker.sandbox.resource_limits import (
    MAX_TIMEOUT_SECONDS,
    get_resource_config,
)


class TestResourceLimits:
    def test_python_defaults_are_reasonable(self):
        cfg = get_resource_config("python")
        assert cfg.memory_mb == 256
        assert cfg.cpu_quota == 0.5
        assert cfg.pids_limit == 64
        assert cfg.network_enabled is False
        assert cfg.output_max_bytes == 1_048_576

    def test_java_gets_higher_memory_than_python(self):
        assert get_resource_config("java").memory_mb > get_resource_config("python").memory_mb

    def test_bash_gets_lower_pids_limit(self):
        bash = get_resource_config("bash").pids_limit
        for lang in ("python", "java", "go", "rust", "ruby", "javascript", "typescript"):
            assert bash <= get_resource_config(lang).pids_limit
        # And strictly lower than the general-purpose runtimes.
        assert bash < get_resource_config("python").pids_limit

    def test_custom_timeout_clamped_to_max(self):
        cfg = get_resource_config("python")
        assert cfg.clamp_timeout(10_000) == min(cfg.timeout_seconds, MAX_TIMEOUT_SECONDS)
        assert cfg.clamp_timeout(None) == cfg.timeout_seconds
        assert cfg.clamp_timeout(0) == cfg.timeout_seconds  # non-positive → ceiling, never 0
        assert cfg.clamp_timeout(-5) == cfg.timeout_seconds
        assert cfg.clamp_timeout(3) == 3

    def test_network_disabled_by_default(self):
        for lang in ("python", "javascript", "java", "go", "ruby", "rust", "bash", "typescript"):
            assert get_resource_config(lang).network_enabled is False

    def test_resource_config_serializes_to_docker_flags(self):
        flags = get_resource_config("python").to_docker_flags()
        assert "--memory=256m" in flags
        assert "--memory-swap=256m" in flags  # swap disabled (== memory)
        assert "--cpus=0.5" in flags
        assert "--pids-limit=64" in flags
        assert "--network=none" in flags
        assert any(f.startswith("--tmpfs=/tmp:") and "noexec" in f for f in flags)
        # ulimits appear as separate --ulimit <value> pairs.
        assert "--ulimit" in flags

    def test_output_limit_prevents_log_flooding(self):
        for lang in ("python", "java", "bash"):
            assert get_resource_config(lang).output_max_bytes <= 1_048_576

    def test_compiled_languages_get_more_time(self):
        for lang in ("java", "go", "rust"):
            assert (
                get_resource_config(lang).timeout_seconds
                >= get_resource_config("python").timeout_seconds
            )
