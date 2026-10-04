# Benchmark harness

Reproducible measurements of the sandbox: per-language latency, end-to-end API latency,
concurrency throughput, hardening overhead versus a bare `docker run`, timeout accuracy, memory
footprint, and the real-Docker security suite. Results and interpretation are in
[`docs/BENCHMARKS.md`](../docs/BENCHMARKS.md); raw data (JSON and CSV) is in `results/`.

Standard library only for the load generator; the executor benchmarks import the repo's
`worker` package. Python 3.12 with `pip install -r worker/requirements.txt -r tests/requirements.txt`.

## Files

| File | Purpose |
|---|---|
| `common.py` | percentile (nearest rank), environment capture, `MemAvailable` guard, JSON/CSV writers |
| `bench_executor.py` | `latency`, `overhead`, `timeout` against `SandboxExecutor` and real Docker (no Redis/API) |
| `bench_api.py` | `latency`, `throughput`, `idle-memory` against a running compose stack |
| `bench_security.py` | runs `tests/security` + `tests/integration` and records per-case outcomes |
| `smoke_examples.py` | captures real request/response pairs and Redis invariants (stream emptied, slot released) |
| `docker-compose.bench.yml` | overlay: memory caps on every container, high rate/concurrency limits, fixed JWT secret |
| `results/` | raw outputs of the run documented in `docs/BENCHMARKS.md` |

## Prerequisites

* Runtime images built: `make build-runtimes` (or `docker build -t sandbox-runtime-<lang>:latest runtimes/<lang>`).
* A Redis for the integration tests: e.g.
  `docker run -d --name bench-redis --memory=128m -p 127.0.0.1:16379:6379 redis:7.4-alpine`
  and `export REDIS_URL=redis://127.0.0.1:16379/15` (database 15 is flushed).
* Every script waits until `MemAvailable` is at least `--min-mem-mb` before each phase, so a run
  on a small machine pauses instead of starving it.

## Run

```bash
# 1. real-Docker security + integration suites -> results/security_and_integration.json
python benchmarks/bench_security.py

# 2. executor-level (no network, no queue)
python benchmarks/bench_executor.py latency                 # all 8 languages
python benchmarks/bench_executor.py overhead --runs 20      # python, bash, javascript
python benchmarks/bench_executor.py timeout --runs 5        # infinite loops, T = 1,2,3,5 s

# 3. full stack (api + worker + redis + minio), memory-capped
export API_PORT=18080 REDIS_PORT=16380 MINIO_PORT=19000 MINIO_CONSOLE_PORT=19001 WORKER_CONCURRENCY=4
export SANDBOX_HOST_WORKDIR=/tmp/code-sandbox-work-bench
C="docker compose -f docker-compose.yml -f benchmarks/docker-compose.bench.yml"
$C up -d --build redis minio api worker
python benchmarks/bench_api.py --base http://localhost:18080 idle-memory
python benchmarks/smoke_examples.py --base http://localhost:18080
python benchmarks/bench_api.py --base http://localhost:18080 latency
python benchmarks/bench_api.py --base http://localhost:18080 throughput --concurrency 1 2 4 8 16 --duration 20
$C down -v --remove-orphans
```

## What is measured, exactly

* **Cold vs warm** (`bench_executor.py latency`): *cold* is the first execution of a language in a
  fresh process; *warm* is every later one. Evicting the OS page cache needs root, so "cold" here
  includes seccomp-profile generation and first use of image layers that may already be cached.
  Each sample is reported twice: `total` (perf_counter around `execute`, including image inspect,
  workdir creation, `docker run`, streaming and cleanup) and `container_wall` (the executor's own
  spawn-to-exit time).
* **API latency** (`bench_api.py latency`): client wall time, one request at a time over one
  keep-alive connection; sync = `POST /v1/execute`; async = submit, then `GET /v1/jobs/{id}` every
  20 ms. One warm-up request per language is discarded.
* **Throughput** (`bench_api.py throughput`): closed loop, N threads, each repeatedly doing a sync
  Python hello-world for a fixed duration. `docker stats` of the four stack containers is sampled
  every second and the peak per container is reported.
* **Overhead** (`bench_executor.py overhead`): three variants run interleaved per iteration: (a)
  bare `docker run --rm -v ... image interpreter file` with Docker defaults, (b) exactly the argv
  the executor builds, run through a plain subprocess, (c) `SandboxExecutor.execute`.
* **Timeout accuracy** (`bench_executor.py timeout`): programs that never exit, requested
  `timeout_seconds` 1, 2, 3, 5; reports measured wall time and overshoot.

## Caveats

* Percentiles are nearest-rank over small samples (10-30 per cell): p95/p99 are the largest or
  second-largest values, not smooth estimates. Treat them as indicative.
* The host is a shared WSL2 VM; other workloads were running (load average well above the core
  count at times). Numbers show this implementation on this machine under those conditions, not a
  tuned production figure. Re-run on your hardware before comparing.
* `k6` (used by `tests/e2e/load_test.js`) was not available; the stdlib load generator replaces it
  and does not reproduce the k6 scenarios (100-500 virtual users, spike, adversarial mix).
