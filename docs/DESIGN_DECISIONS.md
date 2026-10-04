# Design decisions

ADR-style records of the choices that shape the system. Each lists the context, the decision, the
alternatives that were realistic, and what it costs. "Evidence" points to code or to a measurement
in [BENCHMARKS.md](BENCHMARKS.md). These were reconstructed from the code and its comments; they
are not a record of discussions.

## ADR-001: One fresh container per execution via the Docker CLI

* **Context.** Untrusted code must not persist state or see other jobs. Latency matters but
  isolation matters more.
* **Decision.** `docker run --rm` per job, driven by the `docker` CLI as a subprocess
  (`executor.py`). No container pool, no warm sandboxes, no Docker SDK.
* **Alternatives.** A pool of pre-started containers with `docker exec` (faster, but state leaks
  between jobs and the cleanup story is weak); the Docker Engine API through an SDK (avoids a
  process spawn, adds a dependency and a larger trusted surface); Firecracker/gVisor (stronger,
  heavier to operate).
* **Consequences.** Every job pays container creation (see the `bare docker run` rows in
  BENCHMARKS for the floor). Isolation is simple to reason about and every limit is an explicit CLI
  flag, visible in one function. The CLI must exist in the worker image, which also means the
  worker needs the daemon socket.

## ADR-002: The worker, not Docker, enforces the wall-clock timeout

* **Context.** Docker has no per-container wall-clock limit.
* **Decision.** `asyncio.wait(..., timeout)` around `docker run`, then `docker kill` TERM, grace,
  KILL (`_await_completion`, `_terminate`). `--stop-timeout 2` is a backstop only.
* **Alternatives.** `timeout(1)` inside the container (user code could kill or outrun it);
  container `--ulimit cpu` (CPU time, not wall time, so a sleeping program is unbounded).
* **Consequences.** Timeout accuracy includes container start-up (the clock starts at spawn). A
  program that installs a SIGTERM handler and ignores it costs `SIGTERM_GRACE_SECONDS` extra
  (programs with default handling die on TERM because the sandbox runs under `docker --init`; see
  ADR-011). If the worker dies, nothing enforces the timeout until the reaper removes the
  container after 300 s.

## ADR-003: Redis Streams with a consumer group as the queue

* **Context.** Need durable hand-off, multiple workers, and recovery from worker death.
* **Decision.** `XADD` by the API; `XREADGROUP` by workers; `XACK` only after the result is
  stored; `XPENDING`+`XCLAIM` of entries idle > 60 s; dead-letter stream after 3 deliveries.
* **Alternatives.** Redis lists with `BRPOPLPUSH` (no per-consumer pending tracking); RabbitMQ/SQS
  (extra infrastructure); Celery/RQ (hide the semantics this project wants to show).
* **Consequences.** At-least-once delivery: a job can run twice. Entries embed the source code,
  so they must be deleted after processing - the original code only `XACK`ed, which left every
  submission in Redis forever (fixed in this branch with `XDEL`). Reading is bounded by free pool
  slots so an entry is never "delivered but idle". Redis is a single point of failure; compose runs
  it with AOF `everysec`, so up to ~1 s of acknowledged jobs can be lost on a crash.

## ADR-004: Block-list seccomp for toolchain runtimes

* **Context.** A default-deny allow-list is the strongest posture, but tsx/esbuild, the Go
  toolchain and rustc+linker spawn many helpers and use a broad syscall set that is brittle to
  enumerate (commit `b85638f` moved these three to a block-list, "blocklist seccomp for ts/go/rust").
* **Decision.** Default-deny allow-lists for the five interpreters; default-allow plus a deny list
  of escape primitives for TypeScript, Go and Rust (`BLOCKLIST_LANGUAGES`).
* **Alternatives.** Keep allow-lists and generate them per toolchain with tracing; run toolchains
  in a build stage and execute only the output under a tight profile; use Docker's default
  profile.
* **Consequences.** Weaker for three of eight languages against future syscalls. The other layers
  carry the load. A two-stage compile-then-run split (compile with the block-list, run with an
  allow-list) is the natural next step.

## ADR-005: Two separate language registries (API and worker)

* **Context.** The API validates `language` and serves `GET /v1/languages`; the worker needs
  images, entrypoints and limits.
* **Decision.** `api/src/languages.ts` and `worker/sandbox/image_registry.py` +
  `resource_limits.py` are maintained separately and kept in sync by convention and a checklist
  (`ADDING_LANGUAGE.md`).
* **Consequences.** No shared schema, so drift is possible (a language added only to the API
  validates, is queued, and then fails in the worker with "unsupported language"). The duplication
  of `default_timeout_seconds` / `memory_mb` is currently consistent; nothing enforces it.

## ADR-006: Hot result in Redis, best-effort archive in MinIO

* **Decision.** The record and the result are in Redis (TTL 2 h and 1 h). MinIO gets
  `results/<job>.json` as a cold copy and failures are only logged.
* **Consequences.** MinIO is optional for correctness. Nothing reads the archive back; it is
  write-only storage today. `OutputFile` support exists in the types but no producer.

## ADR-007: Fail open on Redis errors for rate limiting and quotas

* **Decision.** `SlidingWindowRateLimiter` and `acquireSlot` allow the request when Redis errors.
* **Alternatives.** Fail closed (return 503). For a demo a limiter outage should not take the
  service down; for a public deployment it removes the abuse protection exactly when the system
  is already stressed. Because job submission also needs Redis, the practical window is small:
  enqueue fails, so only already-queued work is affected.

## ADR-008: In-house HS256 JWT

* **Decision.** ~90 lines using `node:crypto`, algorithm pinned to HS256, `timingSafeEqual`.
* **Consequences.** No dependency, small surface. No `nbf`/`aud`/`iss`, no key rotation (`kid`),
  no revocation, `exp` optional. Fine as a shared-secret service-to-service token; replace with a
  library plus asymmetric keys for end-user identity.

## ADR-009: Sync endpoint is a Redis poll, not a push

* **Decision.** `POST /v1/execute` polls the job record every 100 ms until terminal
  (`waitForTerminal`).
* **Consequences.** Adds up to 100 ms of latency and one Redis read per poll per waiting request.
  A subscription on the existing pub/sub channel would remove the quantisation, at the cost of
  handling missed-message races (subscribe before enqueue, then re-read). Measured effect:
  see the sync-vs-async rows in BENCHMARKS.

## ADR-010: One shared `sandbox-runtime` user (`nobody`, 65534) for all jobs

* **Consequences.** Simple and works with read-only mounts. But `RLIMIT_NPROC` is shared across
  all concurrent sandboxes (the cgroup `pids.max` is the real per-container limit) and, without
  user-namespace remapping, a container escape lands as a real unprivileged host uid shared by all
  sandboxes.

## ADR-011: `docker --init` for every sandbox

* **Context.** The timeout ladder sends SIGTERM, waits, then SIGKILL. Measured without an init
  process, SIGTERM never reached unhandled programs because they were PID 1.
* **Decision.** Always pass `--init` (docker-init/tini as PID 1, user program as its child).
* **Alternatives.** Skip TERM and always KILL (simplest, but removes any chance for a handler to
  flush output); ship tini inside each runtime image (more moving parts); accept the delay.
* **Consequences.** One extra process per sandbox (counts against `--pids-limit`), a negligible
  start-up cost, and signal semantics that match what the API documents. Exit status mapping is
  unchanged: tini returns the child's status (143 for TERM, 137 for KILL/OOM).
