# Deployment and operations

How to run the stack, what it needs from the host, how to size it, and how it behaves when it is
stopped or things fail. Complements [RUNBOOK.md](RUNBOOK.md) (incident procedures). Commands were
run on the benchmark host (WSL2, Docker 29.4.3, cgroup v2 with the systemd driver) unless marked
"not tested".

## 1. Topology

| Service (compose) | Image | Role | State |
|---|---|---|---|
| `redis` | `redis:7.4.1-alpine` | queue, records, results, quotas, pub/sub; AOF `everysec` | volume `redis-data` |
| `minio` + `createbuckets` | `cgr.dev/chainguard/minio` (digest-pinned; override with `MINIO_IMAGE`) | result archive bucket | volume `minio-data` |
| `api` | built from `api/` | HTTP + WebSocket gateway, stateless | none |
| `worker` | built from `worker/` | consumes jobs, launches sandboxes through the host Docker socket | none (per-job dirs under `SANDBOX_HOST_WORKDIR`) |
| `prometheus`, `loki`, `promtail`, `grafana` | pinned | observability | volumes |

**Image availability.** The MinIO and `mc` tags pinned in `docker-compose.yml` could not be pulled on
the benchmark date; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#docker-compose-up-fails-pulling-miniomc-or-miniominio).

Runtime images (`sandbox-runtime-<lang>:<tag>`) are not compose services; they must exist in the
host's Docker image store (`make build-runtimes`, or pulled from a registry via
`SANDBOX_IMAGE_PREFIX`).

## 2. Host requirements

* Docker 24+ (tested 29.4.3) with Compose v2; Linux with cgroup v2 recommended. The resource
  sampler also understands cgroup v1 paths but that was not exercised.
* The worker container mounts `/var/run/docker.sock`, `/sys/fs/cgroup` (read-only) and a work
  directory at the **same absolute path** on host and in the container
  (`SANDBOX_HOST_WORKDIR`, default `/tmp/code-sandbox-work`). The path must be identical because
  the bind mount handed to `docker run` is resolved by the *daemon*, on the host.
* Memory budget: `WORKER_CONCURRENCY x max(memory_mb of languages you allow)` plus the services.
  With 4 workers slots and Java/Rust (512 MB) that is 2 GB of sandbox headroom. The sandbox limits
  are per container; the worker does not check host free memory before starting one.
* The kernel must support the seccomp and cgroup features used. On WSL2 all of the escape suite
  passed on kernel 6.18 (see BENCHMARKS).

## 3. Running

### Development / single host

```bash
cp .env.example .env
make build            # 8 runtime images + api + worker; serial, several GB of downloads
make dev-detached     # or: docker compose up -d --build
curl -s localhost:8080/v1/health
```

`make dev` runs `build-runtimes` first. `ALLOW_ANONYMOUS=true` is the dev default; anonymous
callers share a single 10 requests/minute bucket.

### Production overlay (`docker-compose.prod.yml`)

```bash
export IMAGE_REGISTRY=ghcr.io/you IMAGE_TAG=v1.0.0 JWT_SECRET=$(openssl rand -hex 32) \
       CORS_ORIGINS=https://app.example.com GRAFANA_PASSWORD=...
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

What it changes: uses pre-built images (`build: !reset null`), forces `NODE_ENV=production` and
`ALLOW_ANONYMOUS=false`, requires the secrets above, sets memory/CPU limits (api 512 MB, worker
4 GB, redis 1.25 GB, minio 1 GB), sets Redis `maxmemory` with `noeviction`, and unpublishes Redis
and MinIO (fixed in this branch: a plain `ports: []` is merged with the base list and did not
remove them; the overlay now uses `!reset`). Verify with
`docker compose -f docker-compose.yml -f docker-compose.prod.yml config | grep published`.

Caveats (read from the files, **not tested**):
* `deploy.replicas: ${API_REPLICAS:-2}` with a fixed host port `8080:8080` cannot start two API
  containers on one host; front the API with a load balancer and drop the host port mapping, or
  use an orchestrator.
* `SANDBOX_DIGEST_*` are passed through only for python, javascript and bash.
* Prometheus, Grafana, Loki and Promtail stay published on their dev ports.
* There is no TLS termination in the stack. The API sets HSTS and expects an upstream proxy.
  `trust proxy` is `1`: it assumes exactly one proxy hop in front of it. With zero hops a client can
  spoof `X-Forwarded-For` and therefore its apparent IP.

### Scaling

* Vertical: raise `WORKER_CONCURRENCY` (and host RAM/CPU).
* Horizontal: `docker compose up -d --scale worker=N`. Consumer names come from `WORKER_ID`
  (hostname + random suffix), so replicas do not collide. Each worker process also runs its own
  reaper; they are idempotent.
* The API is stateless; all limits live in Redis, so replicas share rate limits and quotas.
* Measured scaling behaviour on the benchmark host is in [BENCHMARKS.md](BENCHMARKS.md); the
  limiting factor there was CPU shared with other workloads, not the software.

## 4. Lifecycle behaviour

| Event | Behaviour |
|---|---|
| `docker compose stop worker` (SIGTERM) | Consume loop stops after its current `XREADGROUP` (<= 5 s), in-flight jobs finish, Redis connection closes. Compose now waits up to 45 s (`stop_grace_period`) before SIGKILL; the Docker default of 10 s would kill jobs longer than that. |
| worker `kill -9` / host crash | Its un-acked entries are reclaimed by another worker after `QUEUE_CLAIM_MIN_IDLE_MS` (60 s) and re-run (at-least-once). Its sandbox containers are not stopped by anyone until the reaper (any worker) removes them after 300 s; the timeout is not enforced in the meantime. |
| Docker daemon down | The affected message is left pending (not acked); the worker keeps consuming and will retry after the idle time, up to `QUEUE_MAX_RETRIES`, after which the job is marked `FAILED`. The job record shows `RUNNING` meanwhile. |
| Redis down | API: rate limiter and quota fail open; submit fails with 503/500 (breaker opens after 5 failures for 5 s); `GET /v1/health` returns 503. Worker: loop backs off 0.5-5 s with jitter. Jobs already running finish but cannot store results until Redis returns. |
| MinIO down | Ignored by the worker (archive is best effort). Health shows `minio: down` without failing. |
| API restart | In-flight HTTP requests are dropped (sync callers get a connection error; the job still runs and can be fetched by id if it was submitted). WebSockets get close code 1001. |

## 5. Observability

* Metrics: API at `/v1/metrics`, worker at `:9100/metrics`. Names are listed in
  [RUNBOOK.md](RUNBOOK.md#31-metrics). Notable fixes in this branch: the API's execution counter is
  now `sandbox_api_executions_total` (it collided with the worker's `sandbox_executions_total` and
  double counted sync jobs), `sandbox_queue_depth` is a real backlog (acked entries are deleted),
  and `sandbox_container_startup_seconds` is now recorded.
* Alerts: `monitoring/alerts.yml` (failure rate > 10%, backlog > 200, OOM storm, slow start-up,
  worker down).
* Logs: JSON to stdout. Promtail ships Docker container logs to Loki; the API redacts code, stdin
  and credentials.
* Tracing: not functional beyond generating a `traceparent` (see
  [OVERVIEW.md](OVERVIEW.md#known-gaps)).

## 6. Host hardening

These are not provided by the repository; they raise the floor under the sandbox:

1. **User-namespace remapping** (`"userns-remap": "default"` in `daemon.json`). Then container uid
   65534 is an unprivileged high uid on the host. Note that it changes ownership semantics for
   bind mounts (the per-job directory must be readable by the remapped uid; it is 0755/0644 so it
   is). Not tested here.
2. **Rootless Docker** or a **socket proxy** for the worker so a worker bug is not host root.
3. **Alternative runtime** (`--runtime=runsc`) for a second kernel boundary: add the flag in
   `_build_command` and install gVisor on the worker host. Not implemented.
4. **Dedicated worker hosts/VMs**, no other workloads, recent kernel, automatic security updates.
5. **Network**: Redis and MinIO on a private network; `requirepass`/ACLs and `rediss://` for Redis.
6. **Secrets**: `JWT_SECRET`, `API_KEYS`, MinIO and Grafana credentials from a secret manager.

## 7. Backups

Redis holds only transient data (TTL 1-2 h) plus the stream; losing it loses queued and running
jobs and in-flight results. MinIO holds write-only result archives. There is nothing to restore
for correctness; back up only what you want to keep for audit (MinIO bucket).
