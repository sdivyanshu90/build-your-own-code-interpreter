"""Executor-level benchmarks (real Docker, no Redis/API): latency, overhead, timeout accuracy.

Drives ``worker.sandbox.executor.SandboxExecutor`` directly, so the numbers isolate the sandbox
engine from queueing and HTTP.

Subcommands
  latency   cold (first run in this process) vs warm (subsequent runs) per language
  overhead  bare `docker run` vs hardened `docker run` flags vs full executor, same program
  timeout   requested timeout vs measured wall time for a CPU-bound infinite loop
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import environment, summarize, wait_for_memory, write_csv, write_json

from worker.config import load_worker_config
from worker.sandbox.executor import SandboxExecutor
from worker.sandbox.image_registry import get_runtime
from worker.sandbox.types import ExecutionRequest

HELLO = {
    "python": "print('hello')",
    "javascript": "console.log('hello')",
    "typescript": "const n: number = 1; console.log('hello', n)",
    "bash": "echo hello",
    "ruby": "puts 'hello'",
    "java": 'public class Main { public static void main(String[] a){ System.out.println("hello"); } }',
    "go": 'package main\nimport "fmt"\nfunc main(){ fmt.Println("hello") }',
    "rust": 'fn main(){ println!("hello"); }',
}
# Runs per language: light interpreters get more samples, heavy toolchains fewer (RAM/time).
WARM_RUNS = {
    "python": 30,
    "javascript": 30,
    "bash": 30,
    "ruby": 30,
    "typescript": 15,
    "java": 12,
    "go": 10,
    "rust": 10,
}


def _executor(workdir: str) -> SandboxExecutor:
    os.environ["SANDBOX_WORKDIR"] = workdir
    return SandboxExecutor(load_worker_config())


async def _timed(
    executor: SandboxExecutor, language: str, code: str, job_id: str, timeout: int = 30
):
    request = ExecutionRequest(language=language, code=code, timeout_seconds=timeout)
    start = time.perf_counter()
    result = await executor.execute(request, job_id)
    return (time.perf_counter() - start) * 1000.0, result


async def cmd_latency(args: argparse.Namespace) -> None:
    wait_for_memory(args.min_mem_mb)
    workdir = tempfile.mkdtemp(prefix="bench-")
    langs = args.languages or list(HELLO)
    out: dict[str, object] = {}
    raw: list[list[object]] = []
    try:
        executor = _executor(workdir)
        for lang in langs:
            wait_for_memory(args.min_mem_mb)
            n_warm = args.runs or WARM_RUNS[lang]
            cold_total, cold_res = await _timed(executor, lang, HELLO[lang], f"b{lang}c0")
            ok = cold_res.status == "COMPLETED" and cold_res.stdout.strip().startswith("hello")
            warm_total, warm_inner = [], []
            for i in range(n_warm):
                total, res = await _timed(executor, lang, HELLO[lang], f"b{lang}w{i}")
                if res.status != "COMPLETED":
                    ok = False
                warm_total.append(total)
                warm_inner.append(float(res.wall_time_ms))
                raw.append([lang, "warm", i, round(total, 1), res.wall_time_ms, res.status])
            raw.append(
                [lang, "cold", 0, round(cold_total, 1), cold_res.wall_time_ms, cold_res.status]
            )
            out[lang] = {
                "all_runs_completed_with_expected_output": ok,
                "cold_total_ms": round(cold_total, 1),
                "cold_container_wall_ms": cold_res.wall_time_ms,
                "warm_total": summarize(warm_total),
                "warm_container_wall": summarize(warm_inner),
                "memory_peak_bytes_last_run": res.memory_bytes,
            }
            print(
                f"{lang:11s} cold={cold_total:7.0f}ms warm p50="
                f"{out[lang]['warm_total']['p50_ms']}ms ok={ok}",
                flush=True,
            )  # type: ignore[index]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    write_json(
        "executor_latency",
        {
            "benchmark": "executor_latency",
            "definition": "total = perf_counter around SandboxExecutor.execute (image inspect + "
            "workdir + docker run + stream + cleanup); container_wall = executor's "
            "own spawn->exit measurement. cold = first run per language in this "
            "process (no page-cache eviction is possible without root).",
            "environment": environment(),
            "results": out,
        },
    )
    write_csv(
        "executor_latency_raw",
        ["language", "phase", "i", "total_ms", "container_wall_ms", "status"],
        raw,
    )


def _bare_cmd(language: str, workdir: str) -> list[str]:
    runtime = get_runtime(language)
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{workdir}:/sandbox:ro",
        runtime.image,
        *runtime.argv_for(f"/sandbox/{runtime.source_filename}"),
    ]


async def _spawn_wait(argv: list[str], stdin: bytes | None = None) -> float:
    start = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return (time.perf_counter() - start) * 1000.0


async def cmd_overhead(args: argparse.Namespace) -> None:
    wait_for_memory(args.min_mem_mb)
    workdir = tempfile.mkdtemp(prefix="bench-")
    out: dict[str, object] = {}
    try:
        executor = _executor(workdir)
        for lang in args.languages or ["python", "bash", "javascript"]:
            runtime = get_runtime(lang)
            codedir = tempfile.mkdtemp(prefix="bench-code-", dir=workdir)
            os.chmod(codedir, 0o755)
            src = Path(codedir) / runtime.source_filename
            src.write_text(HELLO[lang])
            os.chmod(src, 0o644)
            request = ExecutionRequest(language=lang, code=HELLO[lang], timeout_seconds=30)
            hardened_argv = executor._build_command(runtime, request, codedir, "sandbox-bench-h")
            bare_argv = _bare_cmd(lang, codedir)
            # warm-up once each so image/page cache state is comparable
            await _spawn_wait(bare_argv)
            bare, hard, full = [], [], []
            for i in range(args.runs or 30):  # interleaved to spread drift across the variants
                bare.append(await _spawn_wait(bare_argv))
                hardened_argv_i = [*hardened_argv]
                hardened_argv_i[hardened_argv_i.index("--name") + 1] = f"sandbox-bench-h{i}"
                hard.append(await _spawn_wait(hardened_argv_i))
                total, _ = await _timed(executor, lang, HELLO[lang], f"bo{lang}{i}")
                full.append(total)
            out[lang] = {
                "bare_docker_run": summarize(bare),
                "hardened_flags_only": summarize(hard),
                "full_executor": summarize(full),
            }
            b, h, f = (
                out[lang][k]["p50_ms"]
                for k in ("bare_docker_run", "hardened_flags_only", "full_executor")  # type: ignore[index]
            )
            print(f"{lang:11s} bare p50={b}ms hardened p50={h}ms executor p50={f}ms", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    write_json(
        "sandbox_overhead",
        {
            "benchmark": "sandbox_overhead",
            "definition": "bare = `docker run --rm -v code:/sandbox:ro image <interp> file` with "
            "Docker defaults (default bridge network, default seccomp, no limits); "
            "hardened_flags_only = the exact argv SandboxExecutor builds, run via a "
            "plain subprocess (no sampler, no stdin pump, no cleanup); full_executor "
            "= SandboxExecutor.execute. Runs are interleaved.",
            "environment": environment(),
            "results": out,
        },
    )


async def cmd_timeout(args: argparse.Namespace) -> None:
    wait_for_memory(args.min_mem_mb)
    workdir = tempfile.mkdtemp(prefix="bench-")
    rows: list[list[object]] = []
    out: dict[str, object] = {}
    # Two programs: one that dies on SIGTERM (interpreter default handler) is not possible for a
    # CPU spin under PID 1 (no handler => SIGTERM ignored), so also try a trap-installing one.
    programs = {
        "python_spin_default_signals": ("python", "while True:\n    pass\n"),
        "python_spin_handles_sigterm": (
            "python",
            "import signal, sys\nsignal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
            "while True:\n    pass\n",
        ),
        "bash_sleep": ("bash", "sleep 600"),
    }
    try:
        executor = _executor(workdir)
        for name, (lang, code) in programs.items():
            for requested in args.timeouts:
                samples = []
                for i in range(args.runs or 5):
                    total, res = await _timed(
                        executor, lang, code, f"bt{name[:6]}{requested}{i}", timeout=requested
                    )
                    samples.append(total)
                    rows.append(
                        [
                            name,
                            requested,
                            i,
                            round(total, 1),
                            res.wall_time_ms,
                            res.status,
                            res.timed_out,
                        ]
                    )
                out.setdefault(name, {})[str(requested)] = {  # type: ignore[union-attr]
                    "requested_ms": requested * 1000,
                    "measured_total": summarize(samples),
                    "overshoot_p50_ms": round(summarize(samples)["p50_ms"] - requested * 1000, 1),
                    "overshoot_max_ms": round(max(samples) - requested * 1000, 1),
                }
                print(
                    f"{name:30s} T={requested}s p50={summarize(samples)['p50_ms']}ms "
                    f"max={summarize(samples)['max_ms']}ms",
                    flush=True,
                )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    write_json(
        "timeout_accuracy",
        {
            "benchmark": "timeout_accuracy",
            "definition": "measured_total = perf_counter around SandboxExecutor.execute for a "
            "program that never exits; overshoot = measured - requested. SIGTERM grace "
            "is the worker default (SIGTERM_GRACE_SECONDS=2).",
            "environment": environment(),
            "results": out,
        },
    )
    write_csv(
        "timeout_accuracy_raw",
        ["program", "requested_s", "i", "total_ms", "container_wall_ms", "status", "timed_out"],
        rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("latency", "overhead", "timeout"):
        p = sub.add_parser(name)
        p.add_argument("--languages", nargs="*")
        p.add_argument("--runs", type=int, default=0)
        p.add_argument("--min-mem-mb", type=int, default=1500)
        if name == "timeout":
            p.add_argument("--timeouts", nargs="*", type=int, default=[1, 2, 3, 5])
    args = parser.parse_args()
    asyncio.run(
        {"latency": cmd_latency, "overhead": cmd_overhead, "timeout": cmd_timeout}[args.cmd](args)
    )


if __name__ == "__main__":
    main()
