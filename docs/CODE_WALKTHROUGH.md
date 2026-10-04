# Code walkthrough

A guided tour of every directory and the functions that matter. Line counts are from this
checkout. Paths are relative to the repository root.

```text
api/            TypeScript gateway           (~2.7k lines in src/)
worker/         Python worker + sandbox engine (~2.7k lines)
runtimes/       8 Dockerfiles + generated seccomp JSON
monitoring/     Prometheus, alert rules, Grafana, Loki, Promtail
tests/          unit, integration, security, e2e, helpers
benchmarks/     reproducible benchmark harness and raw results
docs/           this handbook
.github/        CI, security sweep, release
```

## 1. API (`api/src`)

Request path: `index.ts` -> `app.ts` middleware chain -> a router -> services.

### `index.ts` (87 lines) - bootstrap
Builds the Express app, attaches the WebSocket server to the same port via `server.on('upgrade')`,
pre-creates the consumer group, installs SIGTERM/SIGINT handlers (close HTTP, close sockets,
`drainInFlight()` for enqueues, close Redis, hard-exit after 15 s) and `uncaughtException`
handling.

### `app.ts` (105) - `createApp()`
Order matters: `trust proxy = 1` -> helmet (CSP `default-src 'none'`) -> CORS allow-list -> JSON
body limit (`MAX_CODE_SIZE + MAX_STDIN + 64 KiB`) -> pino-http with `x-request-id` -> Prometheus
timer -> routers under `/v1` -> `notFoundHandler` -> `errorHandler`.

### `config.ts` (163)
`ConfigSchema` (zod) is the single source of truth for API configuration; `loadConfig()` throws
`Invalid configuration:` with one line per issue. `getConfig()` memoises;
`resetConfigForTests()` clears it. API keys are parsed into `API_KEY_MAP`. See
[CONFIGURATION.md](CONFIGURATION.md).

### `middleware/`
| File | Key exports | Notes |
|---|---|---|
| `auth.ts` | `authenticatePrincipal(headers, queryToken?)`, `authMiddleware`, `getPrincipal`, `AuthError` | Order: `X-API-Key`, then `Authorization: Bearer` (or WS `?token=`), then anonymous if allowed. Transport-agnostic so the WebSocket upgrade reuses it. API-key principal id is `key:` + 16 hex of SHA-256. |
| `rateLimiter.ts` | `SlidingWindowRateLimiter`, `evaluateRateLimit`, `rateLimitMiddleware`, `clientIp` | One Lua script (`ZREMRANGEBYSCORE`, `ZCARD`, conditional `ZADD`+`PEXPIRE`) per scope; user and IP scopes evaluated together; fails open. |
| `validator.ts` | `validateExecutionRequest`, `validateExecuteBody`, `sanitizeFilename` | `.strict()` object: unknown keys are a 400. `timeout_seconds` defaults and clamps. Env names filtered by regex and deny-list. File names reduced to a safe basename. |
| `errorHandler.ts` | `problem()`, `ApiError`, `asyncHandler`, `errorHandler`, `notFoundHandler` | RFC 7807. `asyncHandler` forwards rejections. Maps `CircuitOpenError`/ioredis connection errors to 503 and body-parser errors to 400/413. |

### `routes/`
| File | Endpoint(s) | Flow |
|---|---|---|
| `execute.ts` | `POST /execute`, `POST /execute/async` | `authMiddleware` -> `rateLimitMiddleware` -> `validateExecuteBody` -> `submit()` (acquire concurrency slot -> `enqueueJob`) -> sync: `waitForTerminal` polling every 100 ms, window `min(SYNC_EXECUTION_TIMEOUT_SECONDS, timeout+5) s`, `408` if still running (the job keeps running), else `200` with the result. |
| `jobs.ts` | `GET/DELETE /jobs/:id` | ULID regex guard, owner check, `DELETE` sets `sandbox:cancel:<id>` (TTL 120 s) and optimistically rewrites the record to `KILLED` in a `MULTI`, then releases the slot. |
| `languages.ts` | `GET /languages` | Static list from `languages.ts`. Public. |
| `health.ts` | `GET /health`, `GET /metrics` | Health pings Redis and MinIO; returns 503 only if Redis is down. Metrics refresh `sandbox_queue_depth` on scrape. |

### `services/`
| File | Purpose |
|---|---|
| `jobQueue.ts` | `ensureConsumerGroup`, `enqueueJob` (one `MULTI`: `XADD` payload + `SET` record with 2 h TTL, inside the breaker), `drainInFlight`. |
| `quota.ts` | Per-user concurrency: Lua acquire on a sorted set (score = timestamp, TTL = `MAX_TIMEOUT+10 s`), `releaseSlot` = `ZREM`. |
| `resultStore.ts` | `getJobRecord`, `getResult`, `waitForTerminal`, MinIO helpers (health, presign). |
| `redis.ts` | Lazy command and subscriber clients, `createRedisConnection` (one per WebSocket), `CircuitBreaker`/`redisBreaker`. |
| `jwt.ts` | `signJwt`, `verifyJwt` (HS256, pinned alg, `timingSafeEqual`, optional `exp`). |
| `websocket.ts` | `handleUpgrade` (auth before the 101), `handleConnection` (one `start` frame -> validate -> rate limit -> quota -> subscribe -> enqueue -> forward worker frames), heartbeat, backpressure at 4 MiB, 120 s hard lifetime. |

### `telemetry/`, `types/`, `languages.ts`
`logger.ts` (pino, redaction), `metrics.ts` (`prom-client`, names prefixed `sandbox_api_` except
`sandbox_executions_submitted_total` and `sandbox_queue_depth`), `tracing.ts` (generates a W3C
`traceparent`; spans only logged). `types/index.ts` is the wire contract: `LANGUAGES`,
`JOB_STATUSES`, `ExecutionRequest/Result`, `JobRecord`, `StreamMessage` (worker -> API pub/sub),
`ServerFrame`/`ClientFrame` (WebSocket), and `REDIS_KEYS`. `languages.ts` is the API-side runtime
registry.

## 2. Worker (`worker/`)

### `worker.py` (~300) - `WorkerDaemon`
`run()` ensures the bucket and group, starts the reaper, runs `_consume_loop()`, and on exit
drains in-flight tasks. `_consume_loop()` reads at most `min(read_count, free slots)` messages
(after first claiming stale ones). `_dispatch()` creates a task per message. `_process()`:

1. delivery count > `max_retries` -> finalise `FAILED` ("abandoned after repeated delivery
   failures"), dead-letter;
2. parse request (`malformed_payload` -> finalise `FAILED`, dead-letter);
3. if the cancel flag is set -> `KILLED`;
4. `_run_job()` = `set_running`, publish `RUNNING`, start the cancel watcher (polls every 0.5 s),
   `executor.execute(...)` with live-output callback, `store_terminal`, publish `done`, metrics;
5. release the concurrency slot, `XACK`+`XDEL`. `DockerUnavailableError` is the only error that
   leaves the entry pending (it will be reclaimed after the idle time and retried).

### `sandbox/`
| File | Contents |
|---|---|
| `executor.py` (~580) | `SandboxExecutor` (see [SANDBOX.md](SANDBOX.md)); `_OutputCapture`; injectable seams `spawn`, `simple`, `memory_reader`, `cpu_reader`, `clock`, `startup_observer` that let the unit tests run without Docker. |
| `image_registry.py` | `RuntimeConfig`, `get_runtime`, `list_runtimes`; per-language entry point and source filename; image/digest from env. |
| `resource_limits.py` | `ResourceConfig` (+ `clamp_timeout`, `to_docker_flags`) and the per-language `_DEFAULTS`. |
| `seccomp.py` (~560) | Syscall sets, `build_profile`, `profile_path`, CLI used by `make seccomp-regen`. |
| `cleanup.py` | `ContainerReaper`, `should_reap`, relative-age parsing of `docker ps` output. |
| `constants.py` | Redis key names and statuses. Must mirror `api/src/types/index.ts`. |
| `types.py` | `ExecutionRequest`/`ExecutionResult` dataclasses with `from_dict`/`to_dict`. |
| `errors.py` | `SandboxError` and subclasses with stable `code`s. |

### `queue/`, `storage/`, `telemetry/`
`consumer.py` - `QueueConsumer`: `ensure_group`, `read`, `claim_stale` (`XPENDING` idle filter +
`XCLAIM`, surfacing the delivery count), `ack` (`XACK`+`XDEL`), `dead_letter`, `queue_depth`
(`XLEN`). `publisher.py` - pub/sub frames mirroring `StreamMessage`. `storage/result_store.py` -
read-modify-write of the job record preserving TTL (`KEEPTTL`), result key with 1 h TTL,
best-effort MinIO archive, `release_slot`. `telemetry/` - JSON logging (user output never logged)
and Prometheus metrics (labels bounded to language/status).

## 3. Runtimes (`runtimes/<lang>/`)
`Dockerfile` (hardened base, non-root, tools removed) and `seccomp-profile.json` (reference copy
of the generated profile; the worker generates its own at run time). See SANDBOX.md section 6.

## 4. Monitoring (`monitoring/`)
`prometheus.yml` (DNS-SD scrape of `api:8080/v1/metrics` and `worker:9100`), `alerts.yml` (five
rules: failure rate, queue backlog, OOM storm, slow start-up, worker down), Grafana provisioning
and `sandbox-overview.json`, Loki and Promtail configs.

## 5. Tests (`tests/`)
See [TESTING.md](TESTING.md). `conftest.py` hosts the `FakeDocker` harness; `helpers/fakeRedis.ts`
is an in-memory Redis including faithful ports of the two Lua scripts.

## 6. CI (`.github/workflows`)
`ci.yml` (lint API/worker, vitest + coverage gate, pytest + coverage gate, image builds, real
Docker integration + security suites), `security.yml` (nightly Trivy for all images, `npm
audit`/`pip-audit`, ZAP baseline), `release.yml` (tag -> push images, sign API/worker with
cosign, e2e smoke, GitHub release).

## 7. Request lifecycle in one paragraph
Client -> `POST /v1/execute` -> auth (JWT/API key) -> sliding-window rate limit (Redis Lua) ->
zod validation -> concurrency slot (Redis Lua) -> `MULTI{XADD job, SET record PENDING}` ->
API polls the record. Worker `XREADGROUP` -> `set_running` -> `docker run` (hardened) -> stream
output to pub/sub -> `docker kill` ladder on timeout -> `SET result`, patch record terminal ->
`XACK`+`XDEL` -> release slot. API poll sees terminal -> `200` + result. Diagrams in
[ARCHITECTURE.md](ARCHITECTURE.md).
