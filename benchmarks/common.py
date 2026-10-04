"""Shared helpers for the benchmark harness: stats, environment metadata, memory guard, I/O."""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent / "results"
REPO_ROOT = Path(__file__).resolve().parent.parent


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation) so every reported value is a real sample."""
    if not samples:
        return math.nan
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarize(samples_ms: list[float]) -> dict[str, float | int]:
    """Summary statistics for a list of millisecond samples."""
    if not samples_ms:
        return {"n": 0}
    mean = sum(samples_ms) / len(samples_ms)
    return {
        "n": len(samples_ms),
        "min_ms": round(min(samples_ms), 1),
        "p50_ms": round(percentile(samples_ms, 50), 1),
        "p95_ms": round(percentile(samples_ms, 95), 1),
        "p99_ms": round(percentile(samples_ms, 99), 1),
        "max_ms": round(max(samples_ms), 1),
        "mean_ms": round(mean, 1),
    }


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # pragma: no cover - best effort metadata
        return "unknown"


def environment() -> dict[str, Any]:
    """Describe the machine and software the numbers were produced on."""
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    return {
        "date_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]),
        "cpu_model": cpu,
        "cpu_count": os.cpu_count(),
        "mem_total_mb": mem_kb // 1024,
        "kernel": platform.release(),
        "docker_server": _run(["docker", "version", "--format", "{{.Server.Version}}"]),
        "docker_cgroup": _run(
            ["docker", "info", "--format", "{{.CgroupVersion}} {{.CgroupDriver}}"]
        ),
        "python": platform.python_version(),
        "note": "shared WSL2 machine; other workloads may run concurrently (see README caveats)",
    }


def mem_available_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) // 1024
    return 0


def wait_for_memory(min_mb: int, timeout_s: int = 900) -> None:
    """Block until MemAvailable >= min_mb (so a benchmark never starts on a starved host)."""
    deadline = time.monotonic() + timeout_s
    while mem_available_mb() < min_mb:
        if time.monotonic() > deadline:
            raise RuntimeError(f"MemAvailable stayed below {min_mb} MB for {timeout_s}s")
        time.sleep(5)


def write_json(name: str, payload: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    return path


def write_csv(name: str, header: list[str], rows: list[list[Any]]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path
