# Code Interpreter Sandbox

A **self-hosted, multi-tenant service that safely executes untrusted code** in disposable,
heavily-restricted containers — the capability behind AI coding assistants, online judges, and
notebook "run" buttons, built to run on infrastructure *you* own.

Submit code over HTTP or WebSocket; get back `stdout`, `stderr`, exit code, and resource metrics.
A malicious submission **cannot** escape the sandbox, exhaust the host, reach the network, read
another tenant's data, or persist anything across runs.

```
   ┌────────┐  HTTPS/WSS   ┌─────────────┐   Redis Streams   ┌──────────────┐   docker run (hardened)   ┌─────────────┐
   │ Client │ ───────────▶ │ API Gateway │ ───────────────▶ │ Worker Pool  │ ───────────────────────▶ │  Ephemeral  │
   │  SDK   │ ◀─────────── │  (Node/TS)  │ ◀── pub/sub ───── │  (Python)    │  --network none           │  Sandbox    │
   └────────┘   result/WS  └─────────────┘   live output     └──────────────┘  --read-only --cap-drop   │  (untrusted)│
                                 │                                  │           ALL --seccomp --user      └─────────────┘
                            Redis (queue/KV/pubsub)          MinIO (artifacts)   nobody --pids-limit …
                                 │
                       Prometheus · Grafana · Loki  (metrics, dashboards, logs)
```

Full documentation: **[`docs/`](docs/README.md)** - overview, architecture diagrams, code
walkthrough, sandbox internals, threat model, API, configuration, benchmarks, testing,
deployment, troubleshooting.

## Features

- **Defence in depth** — Linux namespaces, read-only rootfs, `--network none`, **seccomp**
  (default-deny allow-lists for Python/JS/Java/Ruby/Bash, a block-list for TypeScript/Go/Rust),
  all capabilities dropped, `no-new-privileges`, cgroups v2 limits (memory/CPU/PIDs), non-root
  `nobody`, and optional digest-pinned images. User-namespace remapping, gVisor and a socket proxy
  are *not* part of the repo (see the [threat model](docs/THREAT_MODEL.md#5-implementation-status-of-controls-claimed-elsewhere)).
- **8 language runtimes** — Python, JavaScript, TypeScript, Java, Go, Ruby, Rust, Bash.
- **Sync, async, and streaming** execution (REST long-poll, job polling, and a WebSocket that
  streams stdout/stderr live).
- **Bounded by construction** — wall-clock timeout (SIGTERM→SIGKILL), OOM containment, output
  caps; a fork bomb or infinite loop degrades only its own job.
- **Multi-tenant controls** — JWT + API-key auth, sliding-window rate limits and concurrency
  quotas by tier, RFC 7807 errors.
- **Observability** — Prometheus metrics, a provisioned Grafana dashboard and alert rules,
  structured JSON logs to Loki. (A W3C `traceparent` is generated at the API but not yet consumed
  by the worker; there is no OTLP export.)
- **Resilient** — at-least-once Redis Streams queue with dead-worker reclaim and a dead-letter
  stream, a Redis circuit breaker in the API, a leaked-container GC reaper, and graceful worker
  drain.
- **Tested** — 262 unit/API-integration tests and 59 real-Docker integration, escape and seccomp
  tests (all passing; benchmark host), ≥90% line / ≥85% branch coverage gates (see [TESTING](docs/TESTING.md)).

## Benchmarks (headline)

Measured on one shared WSL2 laptop (11th-gen i5-1135G7, 8 logical CPUs, 5.8 GB RAM, Docker 29.4.3,
cgroup v2) on 2026-10-04; other workloads were running on the same machine, so read tails with
care. Commands, raw data and caveats: [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md),
[`benchmarks/`](benchmarks/README.md).

Hello-world execution through `SandboxExecutor` against real Docker, warm runs, milliseconds
(executor run 1; `container` = container creation + program + teardown, `total` adds the executor's
docker CLI round trips):

| language | container p50 | total p50 | total p95 | first run (cold) |
|---|---|---|---|---|
| python | 544 | 838 | 1,944 | 1,056 |
| javascript | 556 | 830 | 1,833 | 1,890 |
| bash | 512 | 772 | 1,101 | 733 |
| ruby | 526 | 821 | 5,189 | 850 |
| typescript | 1,192 | 1,478 | 3,640 | 1,591 |
| java | 1,178 | 1,449 | 1,905 | 1,836 |
| go | 1,034 | 1,338 | 1,967 | 2,252 |
| rust | 1,020 | 1,288 | 12,605 | 3,281 |

Through the full stack (API + Redis + one 4-slot worker, memory-capped containers, same laptop): a
Python hello-world via `POST /v1/execute` had p50 1,034 ms / p95 1,917 ms (n=30), about 0.2 s above
the executor alone. Idle memory: API 28 MiB, worker 72 MiB, Redis 36 MiB, MinIO 248 MiB.
**Throughput figures are not reported as a capacity claim**: during those runs the host was
oversubscribed by other workloads (load average 8-30) and hello-world jobs began to time out at 8+
concurrent clients; the raw tables and the explanation are in `docs/BENCHMARKS.md`.

Other measured facts: the hardened `docker run` flag set costs nothing against a bare `docker run`
(about 110-140 ms *faster* at p50, because `--network=none` skips network setup); the executor's
own bookkeeping adds about 0.26-0.31 s (two docker CLI calls); timeout overshoot fell from 2.7-4.4 s
to 0.5-1.2 s after running sandboxes under `docker --init`; all 59 real-container integration,
escape and seccomp tests pass.

## Prerequisites

- **Docker** 24+ and **Docker Compose** v2
- **Node.js** 20+ and **Python** 3.12 (only for running tests / linters locally)
- **k6** (optional, for load testing); **Trivy** (optional, for image scanning)

## Quickstart (5 minutes)

```bash
git clone <repo> && cd code-sandbox
cp .env.example .env                 # dev defaults: ALLOW_ANONYMOUS=true
make build                           # build the 8 runtime images + api + worker
make dev                             # start the full stack (api, worker, redis, minio, monitoring)
```

In another terminal — run some code (anonymous is enabled in dev):

```bash
curl -s -X POST http://localhost:8080/v1/execute \
  -H "Content-Type: application/json" \
  -d '{"language":"python","code":"print(sum(range(11)))"}'
# {"job_id":"01J...","status":"COMPLETED","stdout":"55\n","exit_code":0,...}
```

Try the isolation guarantees:

```bash
# Network is blocked:
curl -s -X POST localhost:8080/v1/execute -H 'Content-Type: application/json' \
  -d '{"language":"python","code":"import socket; socket.create_connection((\"1.1.1.1\",53),3)"}'
# → status FAILED / non-zero exit (no route to host)

# Fork bombs are capped and time out; the service stays responsive afterward.
```

Open **Grafana** at <http://localhost:3000> (admin/admin) for the *Code Interpreter Sandbox —
Overview* dashboard.

## Configuration

Copy `.env.example` → `.env`. Key variables (see `.env.example` for the full annotated list):

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET` | _(dev placeholder)_ | HS256 secret; **must be ≥32 chars and not a weak value**. `openssl rand -hex 32`. |
| `ALLOW_ANONYMOUS` | `true` (dev) | Allow unauthenticated execution. **Set `false` in production.** |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowlist. **Never `*` in production.** |
| `API_KEYS` | _(empty)_ | Machine keys, `key:tier` comma-separated (`tier` = `authenticated`\|`premium`). |
| `MAX_CODE_SIZE_BYTES` | `262144` | Max source size (256 KiB). |
| `MAX_STDIN_BYTES` | `262144` | Max stdin (256 KiB). |
| `DEFAULT_TIMEOUT_SECONDS` / `MAX_TIMEOUT_SECONDS` | `10` / `30` | Wall-clock timeout default and ceiling (the worker additionally caps per language, e.g. 10 s for Python). |
| `RATE_LIMIT_*_PER_MINUTE` | `10`/`60`/`600` | Per-minute limits for anonymous / authenticated / premium. |
| `MAX_CONCURRENT_JOBS` / `_PREMIUM` | `5` / `25` | Per-user concurrent-job quota. |
| `WORKER_CONCURRENCY` | `4` | Max concurrent sandboxes per worker process. |
| `SANDBOX_HOST_WORKDIR` | `/tmp/code-sandbox-work` | **Host** dir shared with the worker for per-job code (must be daemon-visible for the read-only bind mount). |
| `SANDBOX_IMAGE_TAG` | `latest` | Tag of the `sandbox-runtime-<lang>` images. |
| `REDIS_URL`, `MINIO_*` | _(see file)_ | Backing services. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(empty)_ | Accepted but currently has no effect (no OTLP exporter). |

## Supported languages

| id | Runtime | Version |
|----|---------|---------|
| `python` | Python | 3.12 |
| `javascript` | Node.js | 20 |
| `typescript` | TypeScript (tsx) | 5.7 |
| `java` | Java (JEP 330 single-file launch) | 21 |
| `go` | Go | 1.22 |
| `ruby` | Ruby | 3.3 |
| `rust` | Rust | 1.83 |
| `bash` | Bash | 5.2 |

Adding one is a short checklist — see [`docs/ADDING_LANGUAGE.md`](docs/ADDING_LANGUAGE.md).

## Using the API

Full reference: [`docs/API.md`](docs/API.md). The surface:

```
POST   /v1/execute            # synchronous (long-poll ≤ 30 s)
POST   /v1/execute/async      # returns 202 + job_id
GET    /v1/jobs/{job_id}      # poll status + result
DELETE /v1/jobs/{job_id}      # cancel / kill
GET    /v1/languages          # list runtimes
GET    /v1/health             # liveness/readiness
GET    /v1/metrics            # Prometheus
WS     /v1/execute/stream     # live stdout/stderr
```

**Python SDK sketch**

```python
import requests
BASE, H = "http://localhost:8080", {"Authorization": f"Bearer {TOKEN}"}
r = requests.post(f"{BASE}/v1/execute", headers=H,
                  json={"language": "ruby", "code": "puts (1..10).sum"})
print(r.json()["stdout"])  # "55\n"
```

**Node SDK sketch**

```javascript
const r = await fetch("http://localhost:8080/v1/execute", {
  method: "POST",
  headers: { "Authorization": `Bearer ${TOKEN}`, "Content-Type": "application/json" },
  body: JSON.stringify({ language: "javascript", code: "console.log([...Array(11).keys()].reduce((a,b)=>a+b))" }),
});
console.log((await r.json()).stdout); // "55\n"
```

## Security model (summary)

Untrusted code runs inside a single-use container with **no network**, a **read-only root
filesystem**, **all Linux capabilities dropped**, `no-new-privileges`, a **default-deny seccomp**
profile, cgroup memory/CPU/PID limits, a non-root `nobody` user, and a hard wall-clock timeout.
The Docker socket is never exposed to sandboxes. Defeating the sandbox requires chaining several
independent controls, but all sandboxes share the host kernel. Full details, the threat model, what is and
is **not** protected against (shared-kernel and microarchitectural risks, the worker's Docker
socket), and a production hardening checklist are in [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)
and [`docs/SECURITY.md`](docs/SECURITY.md).

## Development

```bash
make install          # install node + python dev deps
make lint             # eslint + tsc, ruff + mypy
make test-unit        # fast unit tests (no external services)
make test-integration # real Docker + Redis (requires built runtime images)
make test-e2e         # against a running compose stack
make security-scan    # Trivy scan all images (HIGH/CRITICAL fail)
make load-test        # k6 load scenarios
make clean            # tear everything down
```

Repository layout:

```
api/        Node/TypeScript HTTP + WebSocket gateway
worker/     Python worker daemon + the sandbox engine (sandbox/, queue/, storage/)
runtimes/   Per-language Dockerfile + generated seccomp profile
monitoring/ Prometheus, Grafana (datasources + dashboard), Loki, Promtail configs
benchmarks/ reproducible benchmark harness + raw results
docs/       handbook (start at docs/README.md)
tests/      unit, integration, e2e, security suites + fixtures
```

## Contributing

1. Fork and branch from `main`.
2. `make lint && make test-unit` must pass; add tests for new behaviour (coverage gates are
   enforced in CI: ≥90% line / ≥85% branch).
3. New language? Follow [`docs/ADDING_LANGUAGE.md`](docs/ADDING_LANGUAGE.md) and run
   `make seccomp-regen` so the committed seccomp profiles stay in sync.
4. Open a PR — CI runs lint, type-check, unit + integration tests, image builds, and a security
   sweep. Report vulnerabilities privately per [`docs/SECURITY.md`](docs/SECURITY.md).

## License

Apache-2.0. See `LICENSE`.
