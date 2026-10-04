# Glossary

| Term | Meaning in this project |
|---|---|
| **Sandbox** | The single-use `docker run` container in which user code executes. Named `sandbox-<job_id>`, labelled `sandbox-managed=1`. |
| **Runtime** | A language's image `sandbox-runtime-<lang>:<tag>` plus its entry point, source filename, seccomp profile and resource envelope (`RuntimeConfig`). |
| **Job** | One submission. Identified by a ULID `job_id`. Has a `JobRecord` (status, request, result) in Redis. |
| **Principal** | The authenticated caller: `{user_id, tier, auth_method}`. Tiers: `anonymous`, `authenticated`, `premium`. |
| **Tier** | Determines rate limit and concurrency quota. |
| **Slot** | One entry in a user's concurrency sorted set (`sandbox:concurrency:<user>`), held from submit until the job ends or the slot's TTL expires. |
| **Stream** | The Redis Stream `sandbox:jobs` the API writes to and workers read. |
| **Consumer group** | `workers`. Each worker process is a consumer named by `WORKER_ID`. |
| **PEL** | Pending-entries list: entries delivered to a consumer but not yet `XACK`ed. |
| **Reclaim** | `XCLAIM` of entries idle longer than `QUEUE_CLAIM_MIN_IDLE_MS` from another consumer (dead-worker recovery). |
| **Dead-letter stream** | `sandbox:jobs:dead`. Entries that exceeded `QUEUE_MAX_RETRIES` deliveries or had malformed payloads. Capped to about 1000 entries. |
| **Hot / cold tier** | Redis (hot, TTL) versus MinIO (cold archive, no TTL set by the code). |
| **Sync / async / stream mode** | `POST /v1/execute` (long-poll), `POST /v1/execute/async` + polling, `WS /v1/execute/stream`. |
| **Allow-list / block-list seccomp** | Default-deny (only named syscalls run) versus default-allow (named dangerous syscalls fail). |
| **needs_exec_build** | Runtime flag for compiled languages that need a writable **and executable** `/build` tmpfs. |
| **Reaper** | `ContainerReaper`: periodic GC of leaked containers and stale work directories. |
| **Circuit breaker** | In the API (`redisBreaker`): after 5 consecutive Redis failures it fails fast for 5 s. In the worker, Docker unavailability is handled by leaving the message pending, not by a breaker. |
| **Problem Details** | RFC 7807 `application/problem+json` error bodies. |
| **Cold / warm (benchmarks)** | Cold = first execution of a language in a fresh benchmark process; warm = subsequent ones. Image layers may already be in the OS page cache in both cases. |
| **TB1-TB4** | Trust boundaries in [THREAT_MODEL.md](THREAT_MODEL.md). |
