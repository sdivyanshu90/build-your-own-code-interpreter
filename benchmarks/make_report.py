"""Render benchmarks/results/*.json into Markdown tables.

Writes results/REPORT.md. With ``--fill TEMPLATE OUT`` also substitutes ``@@NAME@@`` markers in a
template (used for docs/BENCHMARKS.md), so every number in the docs comes from the JSON files and
none is typed by hand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
LANG_ORDER = ["python", "javascript", "bash", "ruby", "typescript", "java", "go", "rust"]


def load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def fmt(v: object) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and abs(v) < 20:
        return f"{v:,.2f}"
    return f"{v:,.0f}" if isinstance(v, int | float) else str(v)


def table(header: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def env_line(data: dict) -> str:
    env = data["environment"]
    return (
        f"{env['cpu_model']}, {env['cpu_count']} logical CPUs, {env['mem_total_mb']} MB RAM, "
        f"kernel {env['kernel']}, Docker {env['docker_server']} (cgroup {env['docker_cgroup']}), "
        f"Python {env['python']}."
    )


def latency_table(data: dict) -> str:
    rows = []
    for lang in LANG_ORDER:
        r = data["results"].get(lang)
        if not r:
            continue
        w, c = r["warm_total"], r["warm_container_wall"]
        peak = r["memory_peak_bytes_last_run"]
        rows.append(
            [
                lang,
                r["cold_total_ms"],
                r["cold_container_wall_ms"],
                w["n"],
                w["p50_ms"],
                w["p95_ms"],
                w["p99_ms"],
                c["p50_ms"],
                c["p95_ms"],
                peak // 1048576 if peak else 0,
            ]
        )
    return table(
        [
            "language",
            "cold total",
            "cold container",
            "warm n",
            "warm total p50",
            "p95",
            "p99",
            "warm container p50",
            "p95",
            "peak MiB",
        ],
        rows,
    )


def overhead_table(data: dict) -> str:
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
    return table(
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


def timeout_table(data: dict) -> str:
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
    return table(
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


def api_table(data: dict) -> str:
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
    return table(
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


def throughput_table(data: dict) -> str:
    rows = []
    for conc, r in data["results"].items():
        lat, peak = r["latency"], r["peak_container_mem_mib"]
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
                peak.get("code-sandbox-minio-1"),
            ]
        )
    return table(
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
            "peak minio MiB",
        ],
        rows,
    )


def sections() -> dict[str, str]:
    out: dict[str, str] = {}
    if d := load("executor_latency"):
        out["ENV"] = env_line(d)
        out["LAT"] = latency_table(d)
    if d := load("executor_latency_before_init"):
        out["LAT_RUN1"] = latency_table(d)
    if d := load("sandbox_overhead"):
        out["OVH"] = overhead_table(d)
    if d := load("sandbox_overhead_before_init"):
        out["OVH_RUN1"] = overhead_table(d)
    if d := load("docker_cli_cost"):
        out["CLI"] = table(
            ["docker CLI call", "n", "p50 ms", "p95 ms"],
            [[k, v["n"], v["p50_ms"], v["p95_ms"]] for k, v in d["results"].items()],
        )
    if d := load("timeout_accuracy_before_init"):
        out["TIMEOUT_BEFORE"] = timeout_table(d)
    if d := load("timeout_accuracy"):
        out["TIMEOUT_AFTER"] = timeout_table(d)
    for suffix, key in (("_run1", "RUN1"), ("_run2", "RUN2")):
        if d := load(f"api_latency{suffix}"):
            out[f"API_{key}"] = api_table(d)
        if d := load(f"api_throughput{suffix}"):
            out[f"THR_{key}"] = throughput_table(d)
    if d := load("executor_concurrency"):
        out["CONC"] = table(
            [
                "concurrency",
                "jobs",
                "seconds",
                "jobs/s",
                "p50 ms",
                "p95 ms",
                "max ms",
                "statuses",
                "load avg start -> end",
            ],
            [
                [
                    k,
                    v["jobs"],
                    v["elapsed_s"],
                    v["throughput_per_s"],
                    v["latency"]["p50_ms"],
                    v["latency"]["p95_ms"],
                    v["latency"]["max_ms"],
                    json.dumps(v["statuses"]),
                    " -> ".join(str(x) for x in v["loadavg1_start_end"]),
                ]
                for k, v in d["results"].items()
            ],
        )
    if d := load("idle_memory"):
        out["IDLE"] = table(["container", "MiB"], [[k, v] for k, v in d["results_mib"].items()])
    if d := load("security_and_integration"):
        c = d["counts"]
        out["SEC"] = (
            f"`bench_security.py` (`tests/security` + `tests/integration`, real containers): "
            f"**{c['passed']} passed, {c['failed']} failed, {c['error']} errors, "
            f"{c['skipped']} skipped** (per-case outcomes in `results/security_and_integration.json`)."
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fill", nargs=2, metavar=("TEMPLATE", "OUT"))
    args = parser.parse_args()
    secs = sections()
    report = "\n\n".join(f"### {k}\n\n{v}" for k, v in secs.items()) + "\n"
    (RESULTS / "REPORT.md").write_text(report)
    if args.fill:
        text = Path(args.fill[0]).read_text()
        for key, value in secs.items():
            text = text.replace(f"@@{key}@@", value)
        Path(args.fill[1]).write_text(text)
    print(report)


if __name__ == "__main__":
    main()
