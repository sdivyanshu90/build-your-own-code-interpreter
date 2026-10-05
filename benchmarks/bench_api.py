# ruff: noqa: B023  (closures below are created and fully joined inside each loop iteration)
"""End-to-end API benchmarks against a running compose stack (stdlib only).

Subcommands
  latency      sync POST /v1/execute and async submit->terminal latency per language
  throughput   closed-loop load (N client threads, sync python hello) at several concurrencies,
               with `docker stats` sampling of the stack's containers during the run
  idle-memory  docker stats of the stack's containers with no load
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_executor import HELLO
from common import environment, summarize, wait_for_memory, write_csv, write_json

DEFAULT_SECRET = "bench-secret-bench-secret-bench-secret-123456"
CONTAINERS = [
    "code-sandbox-api-1",
    "code-sandbox-worker-1",
    "code-sandbox-redis-1",
    "code-sandbox-minio-1",
]


def make_jwt(secret: str, sub: str = "bench-user", tier: str = "premium") -> str:
    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    body = b64(json.dumps({"sub": sub, "tier": tier, "iat": now, "exp": now + 7200}).encode())
    sig = b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


class Client:
    """A tiny keep-alive HTTP client (one per thread)."""

    def __init__(self, base: str, token: str) -> None:
        url = urlparse(base)
        self.host, self.port = url.hostname or "localhost", url.port or 80
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=90)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        payload = json.dumps(body) if body is not None else None
        for attempt in (0, 1):
            try:
                self.conn.request(method, path, body=payload, headers=self.headers)
                resp = self.conn.getresponse()
                data = resp.read()
                return resp.status, (json.loads(data) if data else {})
            except (http.client.HTTPException, OSError):
                self.conn.close()
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=90)
                if attempt:
                    raise
        raise RuntimeError("unreachable")


def docker_stats() -> dict[str, float]:
    """Memory (MiB) per stack container from a one-shot `docker stats`."""
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *CONTAINERS],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    result: dict[str, float] = {}
    for line in out.splitlines():
        row = json.loads(line)
        used = row["MemUsage"].split("/")[0].strip()
        unit_scale = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "B": 1 / 1024 / 1024}
        for unit, scale in unit_scale.items():
            if used.endswith(unit):
                result[row["Name"]] = round(float(used[: -len(unit)]) * scale, 1)
                break
    return result


def wait_ready(base: str, timeout_s: int = 120) -> None:
    url = urlparse(base)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=3)
            conn.request("GET", "/v1/health")
            if conn.getresponse().status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("API did not become healthy")


def cmd_latency(args: argparse.Namespace) -> None:
    wait_ready(args.base)
    token = make_jwt(args.secret)
    client = Client(args.base, token)
    runs = {
        "python": 30,
        "javascript": 30,
        "bash": 30,
        "ruby": 30,
        "typescript": 15,
        "java": 12,
        "go": 10,
        "rust": 10,
    }
    out: dict[str, object] = {}
    raw: list[list[object]] = []
    for lang in args.languages or list(HELLO):
        wait_for_memory(args.min_mem_mb)
        n = args.runs or runs[lang]
        sync_ms, async_ms, bad = [], [], 0
        # discard one request per mode: it measures worker/pool warm-up, not steady state
        client.request("POST", "/v1/execute", {"language": lang, "code": HELLO[lang]})
        for i in range(n):
            t0 = time.perf_counter()
            status, body = client.request(
                "POST", "/v1/execute", {"language": lang, "code": HELLO[lang]}
            )
            dt_ms = (time.perf_counter() - t0) * 1000
            if status != 200 or body.get("status") != "COMPLETED":
                bad += 1
            sync_ms.append(dt_ms)
            raw.append([lang, "sync", i, round(dt_ms, 1), status])
        for i in range(n):
            t0 = time.perf_counter()
            status, body = client.request(
                "POST", "/v1/execute/async", {"language": lang, "code": HELLO[lang]}
            )
            job = body.get("job_id", "")
            final = ""
            while time.perf_counter() - t0 < 90:
                _, rec = client.request("GET", f"/v1/jobs/{job}")
                if rec.get("status") in ("COMPLETED", "FAILED", "TIMEOUT", "KILLED"):
                    final = rec["status"]
                    break
                time.sleep(0.02)
            dt_ms = (time.perf_counter() - t0) * 1000
            if status != 202 or final != "COMPLETED":
                bad += 1
            async_ms.append(dt_ms)
            raw.append([lang, "async_poll20ms", i, round(dt_ms, 1), status])
        out[lang] = {
            "sync": summarize(sync_ms),
            "async_poll_20ms": summarize(async_ms),
            "non_completed": bad,
            "loadavg1_end": round(os.getloadavg()[0], 2),
        }
        print(
            f"{lang:11s} sync p50={out[lang]['sync']['p50_ms']}ms "  # type: ignore[index]
            f"p95={out[lang]['sync']['p95_ms']}ms bad={bad}",
            flush=True,
        )  # type: ignore[index]
    write_json(
        f"api_latency{args.tag}",
        {
            "benchmark": "api_latency",
            "definition": "client-side wall time, one request at a time, single keep-alive "
            "connection; sync = POST /v1/execute; async = POST /v1/execute/async then "
            "GET /v1/jobs/{id} every 20 ms until terminal. One warm-up request per "
            "language is discarded.",
            "stack": {"worker_concurrency": args.worker_concurrency},
            "environment": environment(),
            "results": out,
        },
    )
    write_csv(
        f"api_latency{args.tag}_raw", ["language", "mode", "i", "latency_ms", "http_status"], raw
    )


def cmd_throughput(args: argparse.Namespace) -> None:
    wait_ready(args.base)
    token = make_jwt(args.secret)
    out: dict[str, object] = {}
    raw: list[list[object]] = []
    for conc in args.concurrency:
        wait_for_memory(args.min_mem_mb)
        lat: list[float] = []
        errors = {"non200": 0, "exc": 0}
        lock = threading.Lock()
        stop_at = time.perf_counter() + args.duration
        peak: dict[str, float] = {}
        sampling = True

        def sampler() -> None:
            while sampling:
                try:
                    for name, mib in docker_stats().items():
                        peak[name] = max(peak.get(name, 0.0), mib)
                except Exception:  # best effort
                    pass
                time.sleep(1)

        def worker(_: int) -> None:
            client = Client(args.base, token)
            while time.perf_counter() < stop_at:
                t0 = time.perf_counter()
                try:
                    status, body = client.request(
                        "POST",
                        "/v1/execute",
                        {"language": args.language, "code": HELLO[args.language]},
                    )
                except Exception:
                    with lock:
                        errors["exc"] += 1
                    continue
                dt_ms = (time.perf_counter() - t0) * 1000
                with lock:
                    if status == 200 and body.get("status") == "COMPLETED":
                        lat.append(dt_ms)
                        raw.append([conc, round(dt_ms, 1)])
                    else:
                        errors["non200"] += 1

        sampler_thread = threading.Thread(target=sampler, daemon=True)
        sampler_thread.start()
        load_start = os.getloadavg()[0]
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=conc) as pool:
            list(pool.map(worker, range(conc)))
        elapsed = time.perf_counter() - started
        sampling = False
        sampler_thread.join(timeout=5)
        out[str(conc)] = {
            "client_threads": conc,
            "duration_s": round(elapsed, 1),
            "completed": len(lat),
            "errors": errors,
            "throughput_per_s": round(len(lat) / elapsed, 2),
            "latency": summarize(lat),
            "loadavg1_start_end": [round(load_start, 2), round(os.getloadavg()[0], 2)],
            "peak_container_mem_mib": peak,
        }
        print(
            f"conc={conc:3d} {len(lat)} ok in {elapsed:.0f}s = {len(lat)/elapsed:.2f}/s "
            f"p50={out[str(conc)]['latency'].get('p50_ms')}ms errors={errors}",
            flush=True,
        )  # type: ignore[index,union-attr]
        time.sleep(3)
    write_json(
        f"api_throughput{args.tag}",
        {
            "benchmark": "api_throughput",
            "definition": f"closed loop: each of N threads repeatedly POSTs /v1/execute "
            f"({args.language} hello) for {args.duration}s. Latency = client wall time "
            f"for completed requests. worker_concurrency={args.worker_concurrency}.",
            "stack": {"worker_concurrency": args.worker_concurrency},
            "environment": environment(),
            "results": out,
        },
    )
    write_csv(f"api_throughput{args.tag}_raw", ["client_threads", "latency_ms"], raw)


def cmd_idle_memory(args: argparse.Namespace) -> None:
    wait_ready(args.base)
    time.sleep(5)
    stats = docker_stats()
    print(stats)
    write_json(
        "idle_memory",
        {
            "benchmark": "idle_memory",
            "definition": "docker stats MemUsage (MiB), stack idle, "
            "no job run since start-up beyond the health probe",
            "environment": environment(),
            "results_mib": stats,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--secret", default=DEFAULT_SECRET)
    parser.add_argument("--worker-concurrency", type=int, default=4)
    parser.add_argument("--min-mem-mb", type=int, default=1200)
    parser.add_argument("--tag", default="", help="suffix for result file names, e.g. _run2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("latency")
    p.add_argument("--languages", nargs="*")
    p.add_argument("--runs", type=int, default=0)
    p = sub.add_parser("throughput")
    p.add_argument("--concurrency", nargs="*", type=int, default=[1, 2, 4, 8, 16])
    p.add_argument("--duration", type=int, default=20)
    p.add_argument("--language", default="python")
    sub.add_parser("idle-memory")
    args = parser.parse_args()
    {"latency": cmd_latency, "throughput": cmd_throughput, "idle-memory": cmd_idle_memory}[
        args.cmd
    ](args)


if __name__ == "__main__":
    main()
