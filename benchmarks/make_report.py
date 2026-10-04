"""Render benchmarks/results/*.json into Markdown tables (results/REPORT.md).

Every number in docs/BENCHMARKS.md is copied from this output, never typed by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
LANG_ORDER = ["python", "javascript", "bash", "ruby", "typescript", "java", "go", "rust"]


def load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def fmt(v: object) -> str:
    return "-" if v is None else (f"{v:,.0f}" if isinstance(v, int | float) else str(v))


def table(header: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def main() -> None:
    parts: list[str] = []
    if data := load("executor_latency"):
        env = data["environment"]
        parts.append(
            f"Environment: {env['cpu_model']}, {env['cpu_count']} CPUs, "
            f"{env['mem_total_mb']} MB RAM, kernel {env['kernel']}, Docker "
            f"{env['docker_server']} ({env['docker_cgroup']}), commit {env['git_sha']}, "
            f"{env['date_utc']}\n"
        )
        rows = []
        for lang in LANG_ORDER:
            r = data["results"].get(lang)
            if not r:
                continue
            w, c = r["warm_total"], r["warm_container_wall"]
            rows.append(
                [
                    lang,
                    r["cold_total_ms"],
                    w["n"],
                    w["p50_ms"],
                    w["p95_ms"],
                    w["p99_ms"],
                    c["p50_ms"],
                    c["p95_ms"],
                    r["memory_peak_bytes_last_run"] // 1048576
                    if r["memory_peak_bytes_last_run"]
                    else 0,
                ]
            )
        parts.append(
            "### Executor latency (ms), hello world\n\n"
            + table(
                [
                    "language",
                    "cold total",
                    "warm n",
                    "warm p50",
                    "warm p95",
                    "warm p99",
                    "container p50",
                    "container p95",
                    "peak MiB",
                ],
                rows,
            )
        )
    if data := load("sandbox_overhead"):
        rows = []
        for lang, r in data["results"].items():
            b, h, f = (r[k] for k in ("bare_docker_run", "hardened_flags_only", "full_executor"))
            rows.append(
                [
                    lang,
                    b["n"],
                    b["p50_ms"],
                    b["p95_ms"],
                    h["p50_ms"],
                    h["p95_ms"],
                    f["p50_ms"],
                    f["p95_ms"],
                    round(h["p50_ms"] - b["p50_ms"], 1),
                    round(f["p50_ms"] - h["p50_ms"], 1),
                ]
            )
        parts.append(
            "### Overhead versus bare `docker run` (ms)\n\n"
            + table(
                [
                    "language",
                    "n",
                    "bare p50",
                    "bare p95",
                    "hardened p50",
                    "hardened p95",
                    "executor p50",
                    "executor p95",
                    "hardened - bare (p50)",
                    "executor - hardened (p50)",
                ],
                rows,
            )
        )
    if data := load("timeout_accuracy"):
        rows = []
        for prog, by_t in data["results"].items():
            for t, r in by_t.items():
                m = r["measured_total"]
                rows.append(
                    [
                        prog,
                        t,
                        m["n"],
                        m["p50_ms"],
                        m["max_ms"],
                        r["overshoot_p50_ms"],
                        r["overshoot_max_ms"],
                    ]
                )
        parts.append(
            "### Timeout enforcement (ms)\n\n"
            + table(
                [
                    "program",
                    "requested s",
                    "n",
                    "measured p50",
                    "measured max",
                    "overshoot p50",
                    "overshoot max",
                ],
                rows,
            )
        )
    if data := load("api_latency"):
        rows = []
        for lang in LANG_ORDER:
            r = data["results"].get(lang)
            if not r:
                continue
            s, a = r["sync"], r["async_poll_20ms"]
            rows.append(
                [
                    lang,
                    s["n"],
                    s["p50_ms"],
                    s["p95_ms"],
                    s["p99_ms"],
                    a["p50_ms"],
                    a["p95_ms"],
                    r["non_completed"],
                ]
            )
        parts.append(
            "### End-to-end API latency (ms)\n\n"
            + table(
                [
                    "language",
                    "n",
                    "sync p50",
                    "sync p95",
                    "sync p99",
                    "async p50",
                    "async p95",
                    "non-completed",
                ],
                rows,
            )
        )
    if data := load("api_throughput"):
        rows = []
        for conc, r in data["results"].items():
            lat = r["latency"]
            peak = r["peak_container_mem_mib"]
            rows.append(
                [
                    conc,
                    r["completed"],
                    r["duration_s"],
                    r["throughput_per_s"],
                    lat.get("p50_ms"),
                    lat.get("p95_ms"),
                    lat.get("p99_ms"),
                    r["errors"]["non200"] + r["errors"]["exc"],
                    peak.get("code-sandbox-api-1"),
                    peak.get("code-sandbox-worker-1"),
                    peak.get("code-sandbox-redis-1"),
                ]
            )
        parts.append(
            "### Throughput (sync python hello, closed loop)\n\n"
            + table(
                [
                    "client threads",
                    "completed",
                    "seconds",
                    "req/s",
                    "p50 ms",
                    "p95 ms",
                    "p99 ms",
                    "errors",
                    "peak api MiB",
                    "peak worker MiB",
                    "peak redis MiB",
                ],
                rows,
            )
        )
    if data := load("idle_memory"):
        parts.append(
            "### Idle memory (MiB, docker stats)\n\n"
            + table(["container", "MiB"], [[k, v] for k, v in data["results_mib"].items()])
        )
    if data := load("security_and_integration"):
        c = data["counts"]
        parts.append(
            f"### Security + integration suites\n\npassed {c['passed']}, failed "
            f"{c['failed']}, errors {c['error']}, skipped {c['skipped']}"
        )
    (RESULTS / "REPORT.md").write_text("\n\n".join(parts) + "\n")
    print("\n\n".join(parts))


if __name__ == "__main__":
    main()
