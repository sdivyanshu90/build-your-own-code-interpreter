# Configuration reference

Every environment variable the code reads, with type, default and effect. Each row was checked
against the source file named in the section header (not against the older prose docs). Where a
variable is validated but unused, or where two services read the same name differently, that is
called out.

**Precedence.** There is no config file. Both services read `process.env` / `os.environ` once at
start-up. The API refuses to boot on any invalid value (`api/src/config.ts`, `loadConfig`); the
worker only fails on non-integer values for integer settings (`worker/config.py`, `_int`).
Docker Compose supplies values from `.env` (see `docker-compose.yml`).

Contents: [API](#api-service-apisrcconfigts) -
[Worker](#worker-service-workerconfigpy) -
[Runtime registry](#runtime-image-selection-workersandboximage_registrypy) -
[Seccomp](#seccomp-cache) - [Compose-only](#compose-only-variables-envexample) -
[Test and benchmark](#test-and-benchmark-variables) - [Hard-coded limits](#hard-coded-limits-not-configurable)

## API service (`api/src/config.ts`)

Types are the zod schema types. "int>0" means a positive integer, parsed with `z.coerce`.

### Core

| Variable | Type | Default | Effect |
|---|---|---|---|
| `NODE_ENV` | `development`\|`test`\|`production` | `development` | Logged on every line. In `production`, `CORS_ORIGINS` containing `*` aborts boot. |
| `PORT` | int 1-65535 | `8080` | Listen port. |
| `HOST` | string | `0.0.0.0` | Bind address. |
| `LOG_LEVEL` | `trace`..`fatal` | `info` | pino level. Request bodies `code` / `stdin` and `Authorization` / `X-API-Key` headers are redacted (`api/src/telemetry/logger.ts`). |
| `OTEL_SERVICE_NAME` | string | `sandbox-api` | `service` field in logs. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | string | `` | **Currently has no export effect.** `recordSpan` logs a debug line either way (`api/src/telemetry/tracing.ts`). See [Known gaps](OVERVIEW.md#known-gaps). |

### Redis and MinIO

| Variable | Type | Default | Effect |
|---|---|---|---|
| `REDIS_URL` | URL, must start `redis://` or `rediss://` | **required** | All queue, KV, rate-limit and pub/sub traffic. |
| `MINIO_ENDPOINT` | string, min 1 | **required** | MinIO **host name only** for the API (`new Client({endPoint, port})`). See the note below. |
| `MINIO_PORT` | int 1-65535 | `9000` | MinIO port. |
| `MINIO_USE_SSL` | `true`\|`false` | `false` | TLS to MinIO. |
| `MINIO_ACCESS_KEY` | string, min 3 | **required** | |
| `MINIO_SECRET_KEY` | string, min 8 | **required** | |
| `MINIO_BUCKET` | string | `sandbox-artifacts` | Bucket probed by `/v1/health`. |

The API only uses MinIO for the health probe and for presigning (which nothing calls today:
`ExecutionResult.files` is always empty). The worker writes `results/<job_id>.json` archives.

### Authentication and tiers

| Variable | Type | Default | Effect |
|---|---|---|---|
| `JWT_SECRET` | string, min 32, not in deny-list | **required** | HS256 key. Deny-list: `secret`, `changeme`, `change-me`, `password`, `jwt-secret`, `your-secret-here`, `dev`, `test` (only exact matches, case-insensitive; the length rule makes most of them moot). |
| `API_KEYS` | CSV of `key:tier` | `` | `tier` is `premium`, anything else (or missing) means `authenticated`. Keys are compared by map lookup; the principal id is `key:` + first 16 hex chars of SHA-256(key). |
| `ALLOW_ANONYMOUS` | `true`\|`false` | `false` | When `true`, requests without credentials run as the shared principal `anonymous` (one rate-limit and concurrency bucket for **all** anonymous callers). |
| `CORS_ORIGINS` | CSV | `http://localhost:3000` | Exact-match allow-list; requests with no `Origin` are allowed. |

JWT claims: `sub` required; `tier: "premium"` selects the premium tier, anything else is
`authenticated`. `exp` is honoured if present but **not required**; `nbf`/`aud`/`iss` are ignored
(`api/src/services/jwt.ts`).

### Request limits (validator, `api/src/middleware/validator.ts`)

| Variable | Type | Default | Effect |
|---|---|---|---|
| `MAX_CODE_SIZE_BYTES` | int>0 | `262144` | UTF-8 byte length of `code`. Also sizes the JSON body limit: `MAX_CODE_SIZE_BYTES + MAX_STDIN_BYTES + 64 KiB` (`app.ts`). |
| `MAX_STDIN_BYTES` | int>0 | `262144` | Byte length of `stdin`. |
| `MAX_FILES` | int>=0 | `8` | Max entries in `files`. |
| `MAX_FILE_SIZE_BYTES` | int>0 | `262144` | Per input file. |
| `MAX_ENV_VARS` | int>=0 | `32` | Extra entries beyond this are silently dropped. |
| `MAX_ENV_VALUE_LENGTH` | int>0 | `4096` | Longer values fail validation. |
| `DEFAULT_TIMEOUT_SECONDS` | int>0 | `10` | Applied when `timeout_seconds` is omitted. |
| `MAX_TIMEOUT_SECONDS` | int>0 | `30` | Requests above it are **clamped** (not rejected). Must be >= `DEFAULT_TIMEOUT_SECONDS`. The worker then clamps again to the per-language ceiling (see [hard-coded limits](#hard-coded-limits-not-configurable)). |
| `SYNC_EXECUTION_TIMEOUT_SECONDS` | int>0 | `30` | Cap of the `POST /v1/execute` long-poll: `min(this, timeout_seconds + 5)`. |

### Rate limiting and quotas

| Variable | Type | Default | Effect |
|---|---|---|---|
| `RATE_LIMIT_ANON_PER_MINUTE` | int>0 | `10` | Sliding window (60 s), per user id. |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | int>0 | `60` | `authenticated` tier. |
| `RATE_LIMIT_PREMIUM_PER_MINUTE` | int>0 | `600` | `premium` tier. Also the floor of the per-IP limit: `max(tierLimit, this)`. |
| `MAX_CONCURRENT_JOBS` | int>0 | `5` | In-flight jobs per user (non-premium). |
| `MAX_CONCURRENT_JOBS_PREMIUM` | int>0 | `25` | Premium. |
| `WORKER_CONCURRENCY` | int>0 | `4` | **Validated by the API but never used by it.** The worker reads its own copy. |

A concurrency slot is a sorted-set member with a TTL of `MAX_TIMEOUT_SECONDS + 10` seconds, so a
slot self-heals even if nothing releases it. The worker releases it when the job finishes; the API
releases it on sync completion and on cancel.

**MinIO endpoint pitfall.** `docker-compose.yml` sets `MINIO_ENDPOINT: minio:9000` for *both*
services. The worker's MinIO client wants `host:port`; the API's wants a bare host plus
`MINIO_PORT`. See [TROUBLESHOOTING](TROUBLESHOOTING.md#health-reports-minio-down-although-minio-is-running)
for the symptom and the fix that shipped with this branch.

## Worker service (`worker/config.py`)

| Variable | Type | Default | Effect |
|---|---|---|---|
| `REDIS_URL` | string | `redis://localhost:6379/0` | Stream, records, results, cancel flags, pub/sub. |
| `MINIO_ENDPOINT` | `host:port` | `localhost:9000` | Result archive. Best effort: failures log a warning and do not fail the job. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | string | `minioadmin` / `minioadmin` | Note the compose default secret is `minioadmin123`. |
| `MINIO_BUCKET` | string | `sandbox-artifacts` | Created at start-up if absent. |
| `MINIO_USE_SSL` | bool (`1,true,yes,on`) | `false` | |
| `WORKER_CONCURRENCY` | int | `4` | Max sandboxes this process runs at once. The consume loop reads at most this many free slots' worth of messages. |
| `WORKER_ID` | string | `<hostname>-<8 hex>` | Redis consumer name. Must be unique per process. |
| `DOCKER_PATH` | string | `docker` | Docker CLI binary. |
| `SANDBOX_WORKDIR` | path | `<tmp>/sandbox-work` | Where per-job code dirs are written (worker's view). |
| `SANDBOX_HOST_WORKDIR` | path | `` | The **daemon's** view of the same directory, used for the `-v` bind mount. Empty means identical (worker on the host). In compose both sides must be the same path. |
| `QUEUE_BLOCK_MS` | int | `5000` | `XREADGROUP BLOCK`. Also the worst-case shutdown latency of the consume loop. |
| `QUEUE_READ_COUNT` | int | `10` | Upper bound per read; effectively `min(this, free slots)`. |
| `QUEUE_CLAIM_MIN_IDLE_MS` | int | `60000` | A pending entry idle longer than this is reclaimed by any worker. |
| `QUEUE_MAX_RETRIES` | int | `3` | A reclaimed entry whose delivery count exceeds this is dead-lettered and its job marked `FAILED`. |
| `SIGTERM_GRACE_SECONDS` | int | `2` | Delay between `docker kill --signal=TERM` and `--signal=KILL` on timeout or cancel. |
| `METRICS_PORT` | int | `9100` | Prometheus exposition (`prometheus_client`). |
| `LOG_LEVEL` | string | `INFO` | Python logging level. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | string | `` | Read into the config object and **never used**. |

Integer parse errors raise `ValueError` at start-up. Bool parsing is permissive (anything not in
`1,true,yes,on` is false).

## Runtime image selection (`worker/sandbox/image_registry.py`)

| Variable | Default | Effect |
|---|---|---|
| `SANDBOX_IMAGE_PREFIX` | `` | Prepended verbatim, e.g. `ghcr.io/acme/`. Include the trailing slash. |
| `SANDBOX_IMAGE_TAG` | `latest` | Image reference is `<prefix>sandbox-runtime-<lang>:<tag>`. |
| `SANDBOX_DIGEST_<LANG>` | `` | e.g. `SANDBOX_DIGEST_PYTHON=sha256:...`. When set, `docker image inspect` must list that digest in `RepoDigests` before every run, else `ImageIntegrityError` -> job `FAILED`. Locally built images have an empty `RepoDigests`, so digest pinning only works for images pulled from a registry. |

The registry is built once at import; `reload_registry()` rebuilds it (used by tests).

## Seccomp cache

| Variable | Default | Effect |
|---|---|---|
| `SECCOMP_CACHE_DIR` | `<tmp>/sandbox-seccomp` | Directory where `profile_path()` writes the generated JSON profile (once per process per language). The Docker **CLI** reads it and embeds it in the create request, so it only needs to be readable by the worker. |

## Compose-only variables (`.env.example`)

These are interpolated by Compose and are not read by application code: `API_PORT`, `REDIS_PORT`,
`MINIO_PORT` (host side), `MINIO_CONSOLE_PORT`, `PROMETHEUS_PORT`, `LOKI_PORT`, `GRAFANA_PORT`,
`GRAFANA_USER`, `GRAFANA_PASSWORD`, `IMAGE_REGISTRY`, `IMAGE_TAG` (prod overlay), `API_REPLICAS`,
`WORKER_REPLICAS`, `REDIS_MAXMEMORY`, `SANDBOX_DIGEST_*` pass-through for python/javascript/bash only
in `docker-compose.prod.yml`.

Compose defaults that differ from the code defaults: `ALLOW_ANONYMOUS=true`,
`MINIO_SECRET_KEY=minioadmin123`, and an insecure dev `JWT_SECRET`.

## Test and benchmark variables

| Variable | Used by | Effect |
|---|---|---|
| `REDIS_URL` | `tests/conftest.py` (`redis_client`) | Real Redis for integration tests; DB 15 is flushed. CI uses `redis://localhost:6379/15`. |
| `SANDBOX_IMAGE_TAG` | `tests/conftest.py` | Which runtime tag the integration tests look for. |
| `WORKER_CONCURRENCY` | `benchmarks/docker-compose.bench.yml` | Worker pool size for a benchmark run. |

## Hard-coded limits (not configurable)

From `worker/sandbox/resource_limits.py` (per language) and constants:

| Language | Memory | CPUs | Timeout ceiling | PIDs | `/tmp` size | Output cap |
|---|---|---|---|---|---|---|
| python | 256 MB | 0.5 | 10 s | 64 | 64 MB | 1 MiB |
| javascript | 256 MB | 0.5 | 10 s | 64 | 64 MB | 1 MiB |
| typescript | 320 MB | 0.75 | 15 s | 96 | 128 MB | 1 MiB |
| java | 512 MB | 1.0 | 20 s | 128 | 128 MB | 1 MiB |
| go | 384 MB | 1.0 | 20 s | 128 | 256 MB (+ `/build` tmpfs of the same size) | 1 MiB |
| ruby | 256 MB | 0.5 | 10 s | 64 | 64 MB | 1 MiB |
| rust | 512 MB | 1.0 | 25 s | 128 | 256 MB (+ `/build`) | 1 MiB |
| bash | 128 MB | 0.5 | 10 s | 32 | 32 MB | 1 MiB |

Other constants: `MAX_TIMEOUT_SECONDS = 60` and `MAX_MEMORY_MB = 1024` in `resource_limits.py`
(`MAX_MEMORY_MB` is declared but not referenced); `nofile=256:256`; `nproc` equals the PID limit;
result TTL 3600 s and job-record TTL 7200 s (`constants.py`, `jobQueue.ts`); WebSocket heartbeat
15 s, backpressure high-water mark 4 MiB, max stream lifetime 120 s, max frame 1 MiB
(`api/src/services/websocket.ts`); reaper interval 60 s and orphan age 300 s
(`worker/sandbox/cleanup.py`); cancel-flag TTL 120 s (`api/src/routes/jobs.ts`); cgroup sampler
interval 50 ms (`executor.py`); breaker: 5 failures, 5 s cool-down (`api/src/services/redis.ts`).

The effective timeout of a job is therefore
`min(request.timeout_seconds clamped by MAX_TIMEOUT_SECONDS, language ceiling)`. For example a
Python request for 30 s runs with 10 s.
