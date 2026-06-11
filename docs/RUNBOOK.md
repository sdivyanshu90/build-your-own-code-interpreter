# Code Interpreter Sandbox — Operational Runbook

> Audience: SRE / platform on-call · Companion to [ARCHITECTURE.md](./ARCHITECTURE.md) · All `make` targets run from `code-sandbox/`.

This runbook covers deploying, operating, monitoring, and recovering the self-hosted
Code Interpreter Sandbox. It is prescriptive: every command and component name matches the
implementation. When a procedure changes the system, prefer the documented `make` target or
compose command over ad-hoc `docker` invocations.

## Component map

| Component    | What it is                                                              | Port  | State / scaling |
|--------------|-------------------------------------------------------------------------|-------|-----------------|
| `api`        | Node/TypeScript HTTP + WebSocket gateway                                | 8080  | Stateless, horizontally scalable |
| `worker`     | Python asyncio daemon that drives Docker sandbox containers             | 9100 (metrics) | Scale with `docker compose up --scale worker=N`; concurrency via `WORKER_CONCURRENCY` |
| `redis`      | Redis 7 — job stream + KV + pub/sub + rate-limit sorted sets            | 6379  | AOF `appendfsync everysec`; pair with a replica for HA |
| `minio`      | S3-compatible artifact store                                            | 9000 / 9001 (console) | Erasure-coded in prod |
| `prometheus` | Metrics scrape + alert rules (`monitoring/alerts.yml`)                  | 9090  | — |
| `grafana`    | Dashboards (`monitoring/grafana/dashboards/sandbox-overview.json`)      | 3000  | — |
| `loki`       | Log store for structured JSON service logs                             | 3100  | — |
| `promtail`   | Ships container logs to Loki                                            | 9080  | — |

**Queue topology.** Jobs flow over a Redis Stream `sandbox:jobs`, consumer group `workers`,
dead-letter stream `sandbox:jobs:dead`. Workers `XREADGROUP` with `BLOCK`, and only `XACK`
**after** the result is durably stored — delivery is **at-least-once**. Stale entries from a
dead worker are reclaimed via `XPENDING` + `XCLAIM` once they have been idle longer than
`QUEUE_CLAIM_MIN_IDLE_MS` (default `60000`ms). A job whose delivery count exceeds
`QUEUE_MAX_RETRIES` (default `3`) is dead-lettered instead of re-run.

**Self-healing.** A `ContainerReaper` (`worker/sandbox/cleanup.py`) runs every 60s and removes
leaked `sandbox-managed`-labelled containers (exited/dead/created, or running past the 300s
orphan threshold) plus stale per-job work dirs. A circuit breaker (`api/src/services/redis.ts`)
guards Redis; the worker treats Docker-unavailable as transient and does **not** ack, so the job
is reclaimed later. When Docker/Redis is down, the API fails fast with `503`.

---

## 1. Deployment

### 1.1 Local development

```bash
cp .env.example .env        # adjust JWT_SECRET, CORS_ORIGINS, etc.
make build                  # build 8 runtimes + api + worker images
make dev                    # build-runtimes + `docker compose up --build`
make test                   # unit + integration + e2e
```

`make dev` foregrounds the full stack; use `make dev-detached` for background and `make down`
to stop (keeping volumes). The defaults in `.env.example` are dev-only: `ALLOW_ANONYMOUS=true`,
a placeholder `JWT_SECRET`, `CORS_ORIGINS=http://localhost:3000`.

Useful targets:

| Target                | Purpose |
|-----------------------|---------|
| `make build-runtimes` | Build the 8 language runtime images |
| `make build-services` | Build the `api` and `worker` images |
| `make test-unit`      | Python + TS unit tests (no external services) |
| `make test-integration` | Integration tests against real Redis + Docker |
| `make test-e2e`       | End-to-end tests against the running compose stack |
| `make test-coverage`  | Unit tests with coverage gates (≥90% line / ≥85% branch) |
| `make lint`           | Ruff + mypy (worker) and ESLint + tsc (api) |
| `make security-scan`  | Trivy scan all built images (fails on HIGH/CRITICAL) |
| `make load-test`      | k6 load-test scenario against the local API |
| `make seccomp-regen`  | Regenerate the static seccomp profiles |
| `make clean`          | Remove containers, volumes, caches, temp files |
| `make install`        | Install node + python dev dependencies |

> `SANDBOX_HOST_WORKDIR` (default `/tmp/code-sandbox-work`) MUST be a real host path the Docker
> daemon can see. The worker bind-mounts per-job code **read-only** into sandboxes from here at an
> identical host:container path, and also mounts the host Docker socket and `/sys/fs/cgroup:ro`.

### 1.2 Staging

Staging runs the same images via the production overrides but on a single host, with real
secrets and a few workers:

```bash
# .env holds real (non-prod-grade) secrets and pinned image refs.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --scale worker=3
```

Required env in staging `.env` (the prod override file fails fast if these are unset):

| Var               | Notes |
|-------------------|-------|
| `IMAGE_REGISTRY`  | e.g. `ghcr.io/your-org` — prod uses pinned, pre-pushed images (no source rebuild) |
| `IMAGE_TAG`       | The image tag to deploy |
| `JWT_SECRET`      | ≥32 chars, not a well-known value (`openssl rand -hex 32`) |
| `CORS_ORIGINS`    | Strict comma-separated allowlist; never `*` |
| `GRAFANA_PASSWORD`| Required by the prod override |

The prod override sets `NODE_ENV=production`, `ALLOW_ANONYMOUS=false`, removes published
Redis/MinIO ports, applies per-service resource limits, and adds Redis `--maxmemory` /
`--maxmemory-policy noeviction`. Build runtime images first (`make build-runtimes` /
`make push-runtimes`) — the prod compose does not build them.

### 1.3 Production

Run on a hardened deployment (Kubernetes or compose on a hardened host):

- **API** sits behind a load balancer / Ingress; `API_REPLICAS` (default `2`) replicas, stateless.
- **Workers** run on a **dedicated, hardened Docker / gVisor node pool** with **node anti-affinity**
  so no two workers share a host failure domain; `WORKER_REPLICAS` (default `4`).
- **Redis** runs with AOF (`appendfsync everysec`) plus a replica for HA.
- **MinIO** runs erasure-coded.
- **Images** are cosign-verified and **digest-pinned**; in compose, pin runtime images by digest
  via the `SANDBOX_DIGEST_<LANG>` env vars (`SANDBOX_DIGEST_PYTHON`, `SANDBOX_DIGEST_JAVASCRIPT`,
  `SANDBOX_DIGEST_BASH`, …) consumed by `image_registry.py`.

Exact production compose command:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Required production env vars (the override aborts startup if any are missing):

| Var               | Required | Effect |
|-------------------|----------|--------|
| `IMAGE_REGISTRY`  | yes      | Source of pinned `code-sandbox-api` / `code-sandbox-worker` images |
| `IMAGE_TAG`       | yes      | Deployed tag |
| `JWT_SECRET`      | yes      | API token signing (≥32 chars) |
| `CORS_ORIGINS`    | yes      | Strict allowlist |
| `GRAFANA_PASSWORD`| yes      | Grafana admin password |
| `API_REPLICAS`    | no (2)   | API horizontal scale |
| `WORKER_REPLICAS` | no (4)   | Worker horizontal scale |
| `WORKER_CONCURRENCY` | no (8 in prod) | Max concurrent sandboxes per worker |
| `REDIS_MAXMEMORY` | no (1gb) | Redis memory ceiling |
| `SANDBOX_DIGEST_*`| recommended | Digest-pin runtime images |
| `MINIO_USE_SSL`   | no       | TLS to the artifact store |

Inject secrets from the platform's secret manager, never from a committed `.env`.

---

## 2. Day-2 operations

### 2.1 Scaling

The scaling **signals** are `sandbox_queue_depth` (rising backlog) and
`sandbox_container_startup_seconds` (cold-start pressure). Scale **vertically first, then
horizontally**:

1. **Vertical** — raise `WORKER_CONCURRENCY` and give the host more CPU/RAM.
   **Invariant:** keep host memory headroom at or above
   `concurrency × memory_mb × 1.3`. `memory_mb` is the per-language cap in
   `worker/sandbox/resource_limits.py` (e.g. Python/JS 256 MB, Java/Rust 512 MB). Never let
   headroom drop below this — the reaper and the OOM-kill margin depend on it.
2. **Horizontal** — add worker replicas:
   ```bash
   docker compose up --scale worker=N            # dev / staging
   # prod: bump WORKER_REPLICAS and re-apply the prod compose, or scale the k8s Deployment.
   ```

The API is stateless — scale it by raising `API_REPLICAS` (or replicas behind the LB). Prometheus
discovers all scaled `api`/`worker` replicas via Docker DNS service discovery, so new replicas are
scraped automatically.

### 2.2 Adding a language

See **`ADDING_LANGUAGE.md`** for the full procedure (new runtime Dockerfile + seccomp profile, a
`ResourceConfig` entry in `worker/sandbox/resource_limits.py`, the API language allowlist, and
`make build-runtimes` / `make seccomp-regen`).

### 2.3 Rolling a new image version

```bash
make build && make security-scan        # build + Trivy gate
make push-runtimes && make push-services # push to $REGISTRY
# bump IMAGE_TAG (and SANDBOX_DIGEST_* in prod) then re-apply:
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Roll the API and workers independently — both are restart-safe. Because delivery is
at-least-once and ack happens only after durable storage, an in-flight job survives a worker
restart (it is reclaimed by a peer). In Kubernetes, use a rolling update with a surge so capacity
is never zero.

### 2.4 Draining a worker gracefully

Send `SIGTERM` (compose `stop` / `docker stop` / k8s pod termination do this). The worker:

1. stops intake (no new `XREADGROUP`),
2. **drains** in-flight jobs to completion (acking each), then
3. closes Redis and exits.

```bash
docker compose stop worker        # or: docker kill --signal=SIGTERM <worker-container>
```

Allow enough termination grace for the longest job (compiled-language ceilings reach ~25s; see
`resource_limits.py`). `SIGTERM_GRACE_SECONDS` (default `2`) is the grace between SIGTERM and
SIGKILL applied to the **sandbox container** on a per-job timeout/cancel — it is not the worker
drain budget.

---

## 3. Monitoring & SLOs

### 3.1 Metrics

Worker metrics (port 9100) and API metrics (`GET /v1/metrics` on 8080):

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `sandbox_executions_total` | counter | `language`, `status` | Executions by terminal status |
| `sandbox_execution_duration_seconds` | histogram | `language` | Wall-clock execution time |
| `sandbox_queue_depth` | gauge | — | Approx pending jobs in the stream |
| `sandbox_container_startup_seconds` | histogram | — | Pickup → container start |
| `sandbox_oom_kills_total` | counter | — | Executions killed by the OOM killer |
| `sandbox_timeout_kills_total` | counter | — | Executions killed by the wall-clock timeout |
| `sandbox_jobs_in_flight` | gauge | — | Jobs executing on this worker |
| `sandbox_reclaimed_jobs_total` | counter | — | Stale jobs reclaimed from dead workers |
| `sandbox_dead_lettered_total` | counter | — | Jobs moved to the dead-letter stream |
| `sandbox_api_http_requests_total` | counter | `method`, `route`, `code` | API requests |
| `sandbox_api_rate_limited_total` | counter | `tier` | Requests rejected by the rate limiter |

### 3.2 Dashboard & datasources

Grafana auto-provisions the **`sandbox-overview`** dashboard
(`monitoring/grafana/dashboards/sandbox-overview.json`) and two datasources: Prometheus
(uid `prometheus`) and Loki (uid `loki`).

### 3.3 Alert rules (`monitoring/alerts.yml`)

| Alert | Condition | Severity |
|-------|-----------|----------|
| `HighExecutionFailureRate` | FAILED ratio > 10% over 5m | warning |
| `QueueBacklogGrowing` | `sandbox_queue_depth > 200` | warning |
| `OOMKillStorm` | OOM-kill rate elevated | warning |
| `SlowContainerStartup` | p95 `sandbox_container_startup_seconds` > 2s | info |
| `WorkerDown` | `up{job="sandbox-worker"} == 0` | critical |

### 3.4 SLOs

| SLO | Target |
|-----|--------|
| Sync end-to-end latency (p95, trivial script incl. container start) | **< 3s** |
| Execution error rate | **< 1%** |

Burn-rate signals: rising `HighExecutionFailureRate` against the error SLO; rising
`SlowContainerStartup` / `sandbox_execution_duration_seconds` against the latency SLO.

### 3.5 Logs (Loki via Promtail)

Services emit single-line JSON; Promtail ships it to Loki. Fields include `job_id`, `language`,
`user_id`, `duration_ms`, `exit_code`, `oom_killed`, `timed_out`, `worker_id`. Example LogQL:

```logql
# Everything for one job, across api + worker
{service=~"api|worker"} | json | job_id="01J..."

# Failed executions in the worker
{service="worker"} | json | status="FAILED"

# OOM-killed jobs by language
{service="worker"} | json | oom_killed="true" | line_format "{{.language}} {{.job_id}}"
```

---

## 4. Incident response runbooks

### 4.1 Worker crash

**Symptoms:** `WorkerDown` (critical) fires; `sandbox_queue_depth` climbs; that worker's
in-flight stream entries are now pending (un-acked).

**Mechanism:** because ack happens only after durable storage, the dead worker's jobs stay
pending. Peer workers reclaim them via `XPENDING` + `XCLAIM` (the loop's `claim_stale`) once they
have been idle past `QUEUE_CLAIM_MIN_IDLE_MS` (default 60s). The reaper removes any container the
dead worker leaked.

**Steps:**
1. Confirm which worker is down (`up{job="sandbox-worker"}`, `WorkerDown` alert).
2. Restart it: `docker compose up -d --scale worker=<N>` (or let k8s reschedule).
3. Watch recovery: `sandbox_reclaimed_jobs_total` should **increment** as peers pick up the
   orphaned jobs, and `sandbox_queue_depth` should **drain** back toward zero.
4. If containers leaked, the reaper clears them within a cycle; verify with
   `docker ps -a --filter label=sandbox-managed=1` (see §4.6 for manual cleanup).

### 4.2 Queue backlog growing

**Symptoms:** `QueueBacklogGrowing` (`sandbox_queue_depth > 200`).

**Steps:**
1. **Scale workers** (§2.1) — add replicas and/or raise `WORKER_CONCURRENCY` within the memory
   headroom invariant.
2. Check **Docker daemon health** on worker nodes and **image-pull latency**
   (`sandbox_container_startup_seconds` p95; `SlowContainerStartup`). Pre-pull / digest-pin
   runtime images to cut cold starts.
3. The API sheds load by design: it returns `429` (rate limit) and steers callers to async
   submission. Confirm `sandbox_api_rate_limited_total` is doing its job rather than the API
   being overwhelmed.
4. Verify the Redis **circuit breaker isn't stuck open** (no sustained `circuit breaker opened`
   logs from the API and `/v1/health` shows `redis: up`); a stuck-open breaker means new jobs
   aren't being enqueued at all.

### 4.3 OOM-kill storm

**Symptoms:** `OOMKillStorm`; `sandbox_oom_kills_total` rate spiking.

**Steps:**
1. Slice `sandbox_oom_kills_total` and `sandbox_executions_total{status="FAILED"}` **by
   `language`** to find the offending runtime.
2. Inspect Loki for the pattern: `{service="worker"} | json | oom_killed="true"` — is it a
   memory-bomb payload pattern, or a too-tight `memory_mb`?
3. If a legitimate workload needs more: temporarily **raise that runtime's `memory_mb`** in
   `worker/sandbox/resource_limits.py` (capped at `MAX_MEMORY_MB = 1024`). If it's abusive
   traffic: **tighten validation** / rate limits instead.
4. **Never** reduce host memory headroom below the reaper's margin
   (`concurrency × memory_mb × 1.3`) — raising per-job memory means you must also reduce
   `WORKER_CONCURRENCY` or add host RAM.

### 4.4 Docker daemon down

**Symptoms:** execution failures on a node; `HighExecutionFailureRate`.

**Mechanism:** the worker treats Docker-unavailable as **transient** and does **not** ack, so
those jobs stay pending with no data loss. On the API path the breaker opens and the API returns
`503`.

**Steps:**
1. Restore the Docker daemon on the affected node (`systemctl restart docker`); cordon/drain the
   node first in k8s if it's flapping.
2. Once healthy, pending jobs are reclaimed and re-run; the API breaker half-opens on the next
   probe and **drains** as calls succeed.
3. Confirm `sandbox_queue_depth` falls and `/v1/health` returns `200`.

### 4.5 Redis down

**Symptoms:** API `503` for queue writes/reads; `circuit breaker opened` logs.

**Mechanism:** the rate limiter **fails open** (documented in `rateLimiter.ts` / ARCHITECTURE
§1.4) — requests are allowed and a warning logged — so a Redis outage does not block traffic on
the rate-limit path, but queue writes fail and the API returns `503`.

**Steps:**
1. Restore Redis (or fail over to the replica). AOF (`appendfsync everysec`) bounds data loss to
   roughly the last **1s** of writes.
2. The API breaker half-opens and recovers automatically; jobs resume.
3. Re-confirm rate limiting is enforcing again (no more fail-open warnings).

### 4.6 Zombie / leaked containers

The reaper normally handles this (exited/dead containers, or any `sandbox-managed` container
running past 300s). For manual cleanup:

```bash
docker ps -a --filter label=sandbox-managed=1                 # inspect
docker ps -aq --filter label=sandbox-managed=1 | xargs -r docker rm -f   # force remove
# or the full sweep (also drops volumes/caches/work dirs):
make clean
```

### 4.7 Dead-letter queue inspection & re-drive

Jobs that exhausted `QUEUE_MAX_RETRIES` (or had malformed payloads) land on
`sandbox:jobs:dead`. Inspect and re-drive:

```bash
# Inspect — each entry carries payload, reason, delivery_count.
redis-cli XRANGE sandbox:jobs:dead - +

# Re-drive: after fixing the root cause, re-publish the original payload to the live stream.
redis-cli XADD sandbox:jobs '*' payload '<the payload JSON from the dead entry>'
```

Re-drive selectively — a payload that was dead-lettered for being malformed will simply be
dead-lettered again. Trim the dead stream once entries are handled
(`XTRIM sandbox:jobs:dead MINID <id>` or `XDEL`).

---

## 5. Backup & disaster recovery

| Tier | Strategy | RPO / notes |
|------|----------|-------------|
| **Redis** | AOF `appendfsync everysec` + periodic RDB snapshots shipped to object storage + a replica for HA | RPO ≈ **1s** (AOF); snapshots are the off-box copy |
| **MinIO** | Erasure-coding + cross-site replication + lifecycle expiry on the artifact bucket | Durability from erasure-coding; geo-redundancy from replication |
| **api / worker** | **No backup** — stateless; redeploy from pinned/digest images | Recreate from `IMAGE_REGISTRY`/`IMAGE_TAG` |

Results live in Redis with a **1h TTL** (`RESULT_TTL_SECONDS = 3600`); they are intentionally
ephemeral, so DR focuses on the durable tiers (Redis AOF/snapshots and MinIO).

**Quarterly DR drill:** restore the latest Redis snapshot + AOF and a MinIO copy into a clean,
isolated environment, bring up the stack, and run `make test-e2e` against it. The drill passes
when e2e is green and a freshly submitted job completes end-to-end.

---

## 6. Routine maintenance

| Task | Cadence | How |
|------|---------|-----|
| **Rotate `JWT_SECRET` / API keys** | Per policy (e.g. quarterly) | Generate `openssl rand -hex 32`; roll `JWT_SECRET` via the secret manager and re-apply prod compose; update `API_KEYS` (`<key>:<tier>`). Overlap old+new during rollover where possible. |
| **Update + re-pin runtime image digests** | With each runtime update | `make build-runtimes` → `make security-scan` → `make push-runtimes`; capture new digests and update `SANDBOX_DIGEST_*`. |
| **Host kernel patches** | Vendor cadence | Drain workers (§2.4), patch + reboot the node, return to service; node anti-affinity keeps capacity during the roll. |
| **Trivy / CVE scanning** | On every build **and** nightly CI | `make security-scan` (fails on HIGH/CRITICAL via `TRIVY_SEVERITY`). |
| **Prune old results** | Continuous (automatic) | Redis result TTL is **1h**; configure a MinIO **lifecycle** rule to expire old artifacts in the bucket. |
| **Regenerate seccomp profiles** | When syscall policy changes | `make seccomp-regen` then rebuild/push runtimes. |

---

### Quick reference

```bash
# Health
curl -s localhost:8080/v1/health | jq .
curl -s localhost:8080/v1/metrics | grep sandbox_
curl -s localhost:9100/metrics   | grep sandbox_      # a worker

# Queue
redis-cli XLEN sandbox:jobs
redis-cli XINFO GROUPS sandbox:jobs
redis-cli XPENDING sandbox:jobs workers
redis-cli XRANGE sandbox:jobs:dead - +

# Containers
docker ps -a --filter label=sandbox-managed=1
```
