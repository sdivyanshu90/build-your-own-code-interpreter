# Troubleshooting and FAQ

Symptom-first. Each entry says how to confirm the cause and how to fix it; causes marked
"(verified)" were reproduced during this audit, others come from reading the code.

## Jobs

### A job stays `PENDING`
1. Is a worker consuming? `docker compose logs worker | tail`; the worker logs `worker started`.
2. `docker compose exec redis redis-cli XINFO GROUPS sandbox:jobs` - `consumers` should be >= 1 and
   `lag`/`pending` tells you what is waiting. The group is created by whoever starts first
   (`XGROUP CREATE ... MKSTREAM`), API or worker.
3. Are all pool slots busy? `sandbox_jobs_in_flight` equals `WORKER_CONCURRENCY` means you are
   saturated; see [BENCHMARKS.md](BENCHMARKS.md) for the measured ceiling.
4. Is the language known to the worker? A language that exists only in the API fails with
   `FAILED` and `stderr: "unsupported language"`, it does not stay pending.

### A job is `FAILED` with `stderr: "sandbox failure"` or `"internal execution error"`
Clients get generic text by design. The cause is in the worker log (`job failed`, field `err`):
`runtime image not found: sandbox-runtime-X:latest` (run `make build-runtimes`, or check
`SANDBOX_IMAGE_TAG`/`SANDBOX_IMAGE_PREFIX`), `image digest mismatch` (a `SANDBOX_DIGEST_*` is set
for a locally built image: locally built images have no `RepoDigests`), `unsafe input filename`.

### `stderr: "job abandoned after repeated delivery failures"`
The entry was delivered more than `QUEUE_MAX_RETRIES` times without ever completing, typically
because the Docker daemon was unreachable or workers kept dying mid-job. Inspect the dead-letter
stream: `redis-cli XRANGE sandbox:jobs:dead - + COUNT 5`. (Before this branch such jobs stayed
`RUNNING` forever.)

### Python job reports `oom_killed: true` but did not allocate much
`oom_killed` is inferred from exit code 137, which is also what a program gets if it (or a child)
is `SIGKILL`ed for any reason, and what you get for `exit 137` in Bash. The memory limit includes
the `/tmp` tmpfs: writing 200 MB to `/tmp` in a 256 MB Python sandbox leaves almost nothing for the
interpreter.

### A timeout takes longer than requested
Typical measured overshoot is 0.5-1.2 s: about 0.15 s of pre-spawn work, then the timeout (the clock
starts when `docker run` is spawned, so container creation counts toward it), then kill and
cleanup (about 0.3-0.4 s). A program that installs a SIGTERM handler and ignores it additionally
costs `SIGTERM_GRACE_SECONDS` (default 2 s) before the SIGKILL. Before this branch *every* program
without a handler paid that grace, because the interpreter was PID 1 and PID 1 ignores signals it
has no handler for; sandboxes now run under `docker --init` (see
[BENCHMARKS.md](BENCHMARKS.md#timeout-enforcement)).

### The requested timeout is ignored
It is clamped twice: by the API to `MAX_TIMEOUT_SECONDS` (30) and by the worker to the language
ceiling (`resource_limits.py`; 10 s for Python). A request for 30 s in Python runs for 10.

### `408 sync-timeout`
The synchronous window (`min(SYNC_EXECUTION_TIMEOUT_SECONDS, timeout_seconds + 5)`) elapsed. The
job is **not** cancelled and keeps running; the response includes `job_id`, so you can still
`GET /v1/jobs/{id}`. Under load this is usually queueing delay, not slow code.

### TypeScript code with type errors "works"
`tsx` strips types without checking them. `tsc` is installed in the image but `run-code` does not
call it.

### Java: `class Main` not found / wrong class
The source file is always `Main.java` and is run with the JEP 330 launcher, which runs the first
top-level class in the file. Put the `main` method in the first class.

### Go or Rust is slow the first time
Each run is a fresh container: Go copies a pre-warmed build cache (`/opt/gocache`) into `/build`;
Rust compiles with `rustc -O` every time. See the per-language numbers in BENCHMARKS.

## API

### `401` vs `403`
No credentials: `401`. Bad credentials (wrong JWT signature/expired/unknown API key): `403`.
Documented behaviour, but unusual - clients that retry on 401 only will not retry on 403.

### `429 rate-limited` immediately in development
Anonymous callers share one 10 requests/minute bucket across all clients (`ALLOW_ANONYMOUS=true`).
Use a JWT (`make` has no helper; see the Python snippet in [API.md](API.md)) or raise
`RATE_LIMIT_ANON_PER_MINUTE`. Remember the IP limit is `max(tier, RATE_LIMIT_PREMIUM_PER_MINUTE)`.

### `429 concurrency-limit` although nothing seems to be running
Slots are released by the worker when a job ends; they otherwise expire after
`MAX_TIMEOUT_SECONDS + 10` s. Slots of jobs that outlive that (queued for a long time) expire
early, so the quota is a soft limit under heavy queueing.

### Malformed JSON returns 500
It does not any more: body-parser errors map to `400`/`413` (fixed in this branch).

### Redis outage: requests hang
They did before this branch (rejections in async handlers were swallowed). Now they get
`503` + `Retry-After` when the circuit is open or the connection is refused.

### Health reports `minio: down` although MinIO is running
Cause (reproduced): the API's MinIO client rejects a `host:port` endpoint (`Invalid endPoint :
minio:9000`), and `docker-compose.yml` passes exactly that to both services. Fixed in this branch
(`api/src/config.ts` accepts both forms). On an older image set `MINIO_ENDPOINT=minio` and
`MINIO_PORT=9000` for the API. MinIO being down does not fail `/v1/health` (only Redis does).

### WebSocket closes immediately
Check the first frame: it must be `{"type":"start","request":{...}}`. Validation errors, rate
limits and concurrency limits arrive as `{"type":"error",...}` frames and the server closes.
Auth failures never complete the upgrade (HTTP 401/403 on the upgrade request).

## Stack start-up

### `docker compose up` fails pulling `minio/mc` or `minio/minio`
Observed on 2026-10-04: `pull access denied for minio/mc` and `minio/minio:RELEASE.2024-11-07T00-52-20Z`
not found on Docker Hub, `401` from quay.io. MinIO has been withdrawing its prebuilt community
images, so the tags pinned in `docker-compose.yml` may be unobtainable. Options: point
`image:` at a MinIO image you can pull or build, or at any S3-compatible store. `createbuckets` is
optional: the worker creates the bucket itself at start-up (`ResultStore.ensure_bucket`). The
benchmark overlay (`benchmarks/docker-compose.bench.yml`) shows the minimal override.

## Worker and Docker

### `docker: Error response from daemon: ... bind source path does not exist`
`SANDBOX_HOST_WORKDIR` is not the same path on the host and in the worker container. Compose mounts
`${SANDBOX_HOST_WORKDIR}:/var/sandbox-work` and sets `SANDBOX_WORKDIR=/var/sandbox-work`, then
tells the CLI to mount `${SANDBOX_HOST_WORKDIR}/<job>` - the path must exist on the **host**.

### `memory_bytes: 0` / `cpu_time_ms: 0` in results
The sampler could not find the container's cgroup. Ensure the worker has
`/sys/fs/cgroup:/sys/fs/cgroup:ro` and that the cgroup path is `system.slice/docker-<id>.scope`
(systemd driver) or `docker/<id>` (cgroupfs driver). Other layouts return zero without failing.

### Containers named `sandbox-*` accumulate
`docker ps -a --filter label=sandbox-managed=1`. The reaper (every 60 s) removes exited ones and
anything older than 300 s. If the worker is down nothing reaps. Manual:
`docker ps -aq --filter label=sandbox-managed=1 | xargs -r docker rm -f`.

### Fork bomb / memory bomb tests pass but the machine is slow
Limits are per container: `--cpus` is a quota and `RLIMIT_NPROC` is per-uid host-wide. With many
concurrent sandboxes the *sum* is what the host feels. Lower `WORKER_CONCURRENCY`.

## Observability

### Dashboard shows doubled execution counts
Fixed: the API used the same metric name as the worker. If you still see it, an old API image is
running; the API metric is now `sandbox_api_executions_total`.

### `sandbox_queue_depth` keeps growing
It was `XLEN` of a stream that never deleted processed entries (also unbounded Redis growth). Now
entries are `XDEL`ed after `XACK`. To clean an old deployment:
`redis-cli XTRIM sandbox:jobs MINID <id of oldest pending>` after checking `XPENDING`.

### No traces in the collector
Expected: OTLP export is not implemented ([OVERVIEW.md](OVERVIEW.md#known-gaps)).

## FAQ

**Can sandboxes get network access?** Not through any supported configuration: `network_enabled`
exists in `ResourceConfig` but no API or env var sets it.

**Can I install packages from user code?** No network and a read-only root; pre-bake packages into
the runtime image.

**Is `POST /v1/execute` exactly-once?** No. Delivery is at-least-once; a job can be re-run if a
worker dies mid-execution.

**How many concurrent executions?** `WORKER_CONCURRENCY x workers`; measured curve in
[BENCHMARKS.md](BENCHMARKS.md#throughput-under-concurrency).

**Where are results stored and for how long?** Redis, 1 h (result) / 2 h (record); a JSON archive
per job in MinIO with no expiry.

**Why Docker CLI subprocesses and not an SDK?** [ADR-001](DESIGN_DECISIONS.md#adr-001-one-fresh-container-per-execution-via-the-docker-cli).
