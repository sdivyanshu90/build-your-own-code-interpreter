"""Per-language resource envelopes and their translation into Docker flags.

Each runtime gets a tuned :class:`ResourceConfig`. Compiled languages (Java, Go, Rust) and the
JVM in particular need more memory and time; Bash gets the strictest PID limit because shell
fork bombs are trivial to write. ``to_docker_flags`` is the single place that converts a config
into the cgroup/ulimit/namespace flags passed to ``docker run``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hard ceilings the system will never exceed regardless of per-language config.
MAX_TIMEOUT_SECONDS = 60
MAX_MEMORY_MB = 1024


@dataclass(frozen=True)
class ResourceConfig:
    """The resource envelope for a single execution.

    Attributes:
        memory_mb: Hard RAM limit (cgroup ``memory``); swap is disabled separately.
        cpu_quota: CPU bandwidth as a fraction/multiple of one core (e.g. 0.5 = half a core).
        timeout_seconds: Wall-clock timeout enforced by the worker (SIGTERM→SIGKILL).
        pids_limit: Max processes/threads (cgroup ``pids.max``) — fork-bomb prevention.
        disk_mb: Writable ``/tmp`` tmpfs size in MB.
        network_enabled: Whether the container gets a network (default False → ``--network none``).
        output_max_bytes: Combined stdout+stderr cap; output beyond this is truncated.
    """

    memory_mb: int
    cpu_quota: float
    timeout_seconds: int
    pids_limit: int
    disk_mb: int
    network_enabled: bool
    output_max_bytes: int

    def clamp_timeout(self, requested: int | None) -> int:
        """Clamp a user-requested timeout into ``[1, min(self.timeout_seconds, MAX)]``."""
        ceiling = min(self.timeout_seconds, MAX_TIMEOUT_SECONDS)
        if requested is None or requested <= 0:
            return ceiling
        return max(1, min(requested, ceiling))

    def to_docker_flags(self) -> list[str]:
        """Translate the config into ``docker run`` resource flags (argv fragments)."""
        flags: list[str] = [
            f"--memory={self.memory_mb}m",
            # Setting memory-swap == memory disables swap entirely (no swap escape hatch).
            f"--memory-swap={self.memory_mb}m",
            f"--cpus={self.cpu_quota}",
            f"--pids-limit={self.pids_limit}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={self.disk_mb}m",
            # File-descriptor and process ulimits as a second layer beneath pids-limit.
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            f"nproc={self.pids_limit}:{self.pids_limit}",
            # Cap single-file size (blocks/units of 1024 bytes) to bound disk writes.
            "--ulimit",
            f"fsize={self.disk_mb * 1024 * 1024}",
        ]
        if not self.network_enabled:
            flags.append("--network=none")
        return flags


# Tuned defaults per language. Values chosen from real runtime footprints with headroom.
_DEFAULTS: dict[str, ResourceConfig] = {
    "python": ResourceConfig(
        memory_mb=256,
        cpu_quota=0.5,
        timeout_seconds=10,
        pids_limit=64,
        disk_mb=64,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "javascript": ResourceConfig(
        memory_mb=256,
        cpu_quota=0.5,
        timeout_seconds=10,
        pids_limit=64,
        disk_mb=64,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "typescript": ResourceConfig(
        # tsc/ts-node compile step needs more headroom than plain node.
        memory_mb=320,
        cpu_quota=0.75,
        timeout_seconds=15,
        pids_limit=96,
        disk_mb=128,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "java": ResourceConfig(
        # The JVM is memory- and thread-hungry; it spawns GC/compiler threads.
        memory_mb=512,
        cpu_quota=1.0,
        timeout_seconds=20,
        pids_limit=128,
        disk_mb=128,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "go": ResourceConfig(
        # `go run` compiles then runs; the toolchain needs CPU and several PIDs.
        memory_mb=384,
        cpu_quota=1.0,
        timeout_seconds=20,
        pids_limit=128,
        disk_mb=256,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "ruby": ResourceConfig(
        memory_mb=256,
        cpu_quota=0.5,
        timeout_seconds=10,
        pids_limit=64,
        disk_mb=64,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "rust": ResourceConfig(
        # rustc is the heaviest compile step of all the runtimes.
        memory_mb=512,
        cpu_quota=1.0,
        timeout_seconds=25,
        pids_limit=128,
        disk_mb=256,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
    "bash": ResourceConfig(
        # Strictest PID cap: shell fork bombs are a one-liner.
        memory_mb=128,
        cpu_quota=0.5,
        timeout_seconds=10,
        pids_limit=32,
        disk_mb=32,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
}


def get_resource_config(language: str) -> ResourceConfig:
    """Return the tuned :class:`ResourceConfig` for a language.

    Raises:
        KeyError: if the language has no configured defaults.
    """
    return _DEFAULTS[language]


def has_resource_config(language: str) -> bool:
    """Return True if defaults exist for the language."""
    return language in _DEFAULTS
