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

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design, threat model, and
isolation layers.

## Features

- **8 layers of defence in depth** — Linux namespaces, read-only rootfs, `--network none`,
  default-deny **seccomp** allowlists per language, all capabilities dropped, cgroups v2 limits
  (memory/CPU/PID), `nobody` + user-namespace remapping, and immutable pinned/signed images.
- **8 language runtimes** — Python, JavaScript, TypeScript, Java, Go, Ruby, Rust, Bash.
- **Sync, async, and streaming** execution (REST long-poll, job polling, and a WebSocket that
  streams stdout/stderr live).
- **Bounded by construction** — wall-clock timeout (SIGTERM→SIGKILL), OOM containment, output
  caps; a fork bomb or infinite loop degrades only its own job.
- **Multi-tenant controls** — JWT + API-key auth, sliding-window rate limits and concurrency
  quotas by tier, RFC 7807 errors.
- **Observability-first** — Prometheus metrics, a provisioned Grafana dashboard, structured JSON
  logs to Loki, and W3C trace-context propagation from HTTP → queue → container.
- **Resilient** — at-least-once queue with dead-worker reclaim and a dead-letter queue, a Docker
  circuit breaker, a leaked-container GC reaper, and graceful shutdown.
- **Tested** — 230+ unit/integration/security tests, ≥90% line / ≥85% branch coverage gates, plus
  a real-Docker container-escape suite and a k6 load test.

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
| `DEFAULT_TIMEOUT_SECONDS` / `MAX_TIMEOUT_SECONDS` | `10` / `30` | Wall-clock timeout default and ceiling. |
| `RATE_LIMIT_*_PER_MINUTE` | `10`/`60`/`600` | Per-minute limits for anonymous / authenticated / premium. |
| `MAX_CONCURRENT_JOBS` / `_PREMIUM` | `5` / `25` | Per-user concurrent-job quota. |
| `WORKER_CONCURRENCY` | `4` | Max concurrent sandboxes per worker process. |
| `SANDBOX_HOST_WORKDIR` | `/tmp/code-sandbox-work` | **Host** dir shared with the worker for per-job code (must be daemon-visible for the read-only bind mount). |
| `SANDBOX_IMAGE_TAG` | `latest` | Tag of the `sandbox-runtime-<lang>` images. |
| `REDIS_URL`, `MINIO_*` | _(see file)_ | Backing services. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(empty)_ | OTLP collector for traces (empty disables export). |

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
The Docker socket is never exposed to sandboxes. Eight independent layers mean defeating the
sandbox requires chaining multiple hardened controls. Full details, the threat model, what is and
is **not** protected against (e.g. microarchitectural side channels), and a production hardening
checklist are in [`docs/SECURITY.md`](docs/SECURITY.md).

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
docs/       ARCHITECTURE, API, SECURITY, ADDING_LANGUAGE, RUNBOOK
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
