# Code Interpreter Sandbox — Architecture & System Design

> Status: **Production design baseline** · Audience: platform, security, and SRE engineers · Last reviewed: 2026-06

---

## 1.1 Executive Summary

### The problem

AI coding assistants, online judges, data-science notebooks, and "run this snippet"
buttons all share one dangerous requirement: **they must execute code they did not write
and cannot trust, on infrastructure they own.** A single naive mistake — running the code
in a subprocess on the host — hands an attacker arbitrary code execution on production
servers, lateral movement into the cluster, and access to every other tenant's data.

This system is a **self-hosted, multi-tenant execution service** that accepts arbitrary
source code over an HTTP/WebSocket API and runs it inside disposable, heavily-restricted
sandboxes. It returns `stdout`, `stderr`, the exit code, and resource metrics, and it
guarantees that a malicious submission cannot escape the sandbox, exhaust the host, reach
the network, read another tenant's data, or persist anything across executions.

It is designed for the team that has outgrown third-party execution APIs (cost, latency,
data-residency, or compliance reasons) and needs to operate the capability themselves.

### Top three non-negotiable constraints

1. **Security / isolation (correctness-critical).** Untrusted code must never gain a
   foothold on the host or in the orchestration plane. Isolation is *defence in depth* —
   no single control (not even the container boundary) is trusted to be sufficient on its
   own. A failure here is a breach, not a bug.
2. **Bounded resource consumption.** Every execution runs under a hard wall-clock timeout,
   a memory cap, a CPU quota, a PID cap, an FD cap, and an output cap. A fork bomb, an
   infinite loop, or a 10 GB allocation must degrade *only that one job*, never the worker
   or its neighbours. The system stays responsive under adversarial load by construction.
3. **Predictable, low-tail latency.** Interactive use (an AI assistant waiting on a
   result) demands a tight latency budget. Target: **p50 < 600 ms** and **p95 < 3 s** for a
   trivial script end-to-end, including container start. Cold-start cost is amortised with
   pre-pulled, pinned images and a warm worker pool; queueing decouples spikes from
   capacity.

### Why not the naive approach

The naive approach — `subprocess.run(user_code)` on the host, maybe with a `timeout` — is
catastrophic and is rejected for concrete reasons:

| Naive behaviour | Consequence | This design |
|---|---|---|
| Runs as the host user, full FS access | Reads `/etc/shadow`, SSH keys, app secrets; writes cron jobs | Read-only rootfs, dropped to `nobody`, user namespace, no host mounts |
| Shares the host network | Exfiltrates data, scans the internal network, pivots | `--network none` by default; egress only via an allow-listed proxy |
| No syscall restriction | `ptrace`, `keyctl`, `mount`, kernel-exploit primitives all reachable | Default-deny seccomp allow-list per runtime |
| Host kernel, host capabilities | One kernel LPE = host root | `--cap-drop ALL`, `no-new-privileges`, user namespaces, optional gVisor |
| `timeout(1)` only | Fork bombs survive (new PIDs), memory bombs OOM the host | cgroups v2: `pids-limit`, `memory`, `cpus`; SIGTERM→SIGKILL ladder |
| In-process with the API | A crash or RCE takes down the service | Strict process, container, and host separation; ephemeral workers |

The design treats the container boundary as **one layer among eight**, assumes any single
layer can fail, and ensures that defeating the system requires chaining multiple
independent, individually-hardened controls.

---

## 1.2 Threat Model

Trust boundaries (outermost → innermost):

```
Internet ─┬─> [API Gateway]  (authn/z, rate limit, validation)   TRUST BOUNDARY 1
          │
          └─> [Redis Streams queue]  (job payloads, no code exec) TRUST BOUNDARY 2
                  │
                  └─> [Worker daemon] (orchestration, host-trusted) TRUST BOUNDARY 3
                          │
                          └─> [Sandbox container] (UNTRUSTED CODE)  TRUST BOUNDARY 4
```

Everything inside Trust Boundary 4 is assumed hostile. The job of the architecture is to
make Boundary 4 hold even when the code inside it is actively trying to break out.

### Attack surface enumeration

| # | Threat | Attack Vector | Severity | Impact | Mitigation (defence in depth) |
|---|--------|---------------|----------|--------|-------------------------------|
| T1 | Arbitrary command execution on host | Malicious user code expects to run on host | **Critical** | Full host compromise | Code only ever runs inside a container as `nobody`; never `exec`'d on host; filename never shell-interpolated (argv array, code written to a file) |
| T2 | Host filesystem escape / read host secrets | `open('/etc/shadow')`, path traversal, `..` in filenames | **Critical** | Secret theft, key exfiltration | `--read-only` rootfs; **no host bind mounts of host paths**; code mounted `:ro` from a per-job temp dir; user-supplied filenames sanitised + path-confined; user namespace; dropped caps |
| T3 | Privilege escalation via kernel exploit | Malicious syscalls trigger a kernel LPE | **Critical** | Host root | `--cap-drop ALL`; `no-new-privileges`; **default-deny seccomp** blocks the syscalls most LPEs need (`keyctl`, `bpf`, `userfaultfd`, `io_uring`, `unshare`, `clone` flags); user namespace; optional gVisor runtime for a second kernel boundary |
| T4 | Sandbox escape via `/proc`, `/sys`, `/dev` | Read/write `procfs`/`sysfs`, `/proc/1`, `/dev/mem` | **Critical** | Info leak, host control | masked `/proc` paths (Docker default), read-only rootfs, dropped caps, seccomp blocks `mount`/`pivot_root`; no `--privileged`, no `/dev` device passthrough |
| T5 | Resource exhaustion (fork bomb, infinite loop, mem bomb) | `:(){ :|:& };:`, `while True`, `'x'*10**10` | **High** | DoS of worker / neighbours | cgroups v2: `--pids-limit`, `--memory`+`--memory-swap` (swap disabled), `--cpus`; wall-clock timeout (SIGTERM→SIGKILL); `--ulimit nproc/nofile`; output byte cap |
| T6 | Network exfiltration / SSRF | `socket.connect()`, HTTP to internal metadata, DNS tunnelling | **High** | Data exfiltration, cloud-metadata theft, internal recon | `--network none` by default (no interfaces but loopback); seccomp can deny `socket`; optional egress only through an allow-list proxy with DNS sinkhole; no link-local `169.254.169.254` reachable |
| T7 | Dependency confusion / supply chain | Code triggers `pip install`, malicious package | **High** | Backdoored runtime, build-time RCE | **No package installation at runtime**; images are immutable, pinned by digest, signed (cosign) and verified before run; no network in sandbox; package mirrors pinned at build time |
| T8 | Co-tenancy side channels | Spectre/Meltdown, cache timing, `/proc` timing | **Medium** | Cross-tenant info leak (bounded) | One container per job (no reuse); ephemeral; KPTI/retpoline host kernel; optional dedicated CPU sets; documented residual risk (see SECURITY.md); gVisor reduces shared-kernel surface |
| T9 | Container breakout via Docker socket | Code finds `/var/run/docker.sock` | **Critical** | Full host control | Docker socket is **never** mounted into sandboxes; worker talks to Docker over a restricted local socket the sandbox cannot see; socket path not present in container FS |
| T10 | Orchestration-plane attack | Malicious payload targets worker/Redis parsing | **High** | Worker RCE, queue poisoning | Strict typed schema validation at API and worker; code is opaque bytes, never `eval`'d by the worker; Redis not exposed publicly; payload size caps |
| T11 | Output flooding / log injection | Print 100 MB, ANSI/log-forging sequences | **Medium** | Disk/log DoS, log spoofing | Hard `output_max_bytes` truncation; structured (JSON) logging escapes control chars; per-job output streamed with backpressure |
| T12 | Denial via slow-loris / queue flooding | Many requests, huge payloads | **High** | Service DoS | Sliding-window rate limits (IP + user), payload size caps, max concurrent jobs per user, queue depth backpressure, circuit breaker on Docker |
| T13 | Auth bypass / token theft | Forged JWT, weak secret, replay | **High** | Quota bypass, impersonation | HS256/RS256 with startup secret-strength validation; short token TTL; API keys hashed at rest; per-key scopes |
| T14 | Time-of-check/time-of-use on code injection | Filename `"; rm -rf /"`, symlink swap | **High** | Worker-side command injection | Code + filenames written via safe file APIs, **passed as argv arrays** (never a shell string); per-job temp dir created with `mkdtemp`, mounted read-only |
| T15 | Container left running / leak | Worker dies mid-job | **Medium** | Resource leak, zombie containers | `--rm`; labelled containers; a reaper GC sweeps orphans by label + age; `XPENDING` reclaim re-runs stuck jobs |

### Defence-in-depth detail for every Critical threat

**T1 Arbitrary command execution — two independent layers**
- L1: User code is *never* executed by the host or the worker process. It is written to a
  file inside a per-job temp directory and the **only** thing that ever runs it is the
  container entrypoint, with the code path passed as an `argv` element (no shell).
- L2: That container runs as `nobody` with all capabilities dropped and
  `no-new-privileges`, so even successful execution is confined.

**T2 Host filesystem escape — three layers**
- L1: `--read-only` root filesystem + writable space limited to a 64 MB `noexec` tmpfs
  `/tmp`. Nothing user code writes can persist or be executed.
- L2: **No host paths are bind-mounted.** The only mount is the per-job code dir, mounted
  read-only. User-supplied filenames are sanitised (`basename`, reject `..`, reject
  absolute) and confined under `/sandbox`.
- L3: User namespace remaps container `root`/`nobody` to an unprivileged host UID, so even
  a hypothetical mount/escape yields no host privilege.

**T3 Kernel-exploit privilege escalation — three layers**
- L1: Default-deny seccomp removes the syscalls that modern LPEs pivot through
  (`bpf`, `keyctl`, `userfaultfd`, `io_uring_setup`, `unshare`, `add_key`, `ptrace`, …).
- L2: `--cap-drop ALL` + `--security-opt no-new-privileges` removes the capabilities most
  exploits assume.
- L3 (optional, production-recommended): run under the **gVisor (`runsc`)** runtime, a
  user-space kernel that intercepts syscalls so the host kernel is never directly reachable.

**T4 `/proc` `/sys` `/dev` escape — two layers**
- L1: Docker masks sensitive `/proc` paths and mounts `/proc` `/sys` restricted; rootfs is
  read-only; no devices passed through.
- L2: seccomp blocks `mount`, `umount2`, `pivot_root`, `chroot`, and capabilities are
  dropped, so re-mounting a writable `proc` is not possible.

**T9 Docker-socket breakout — two layers**
- L1: The Docker socket is never inside any sandbox mount namespace; its path simply does
  not exist in the container.
- L2: The worker reaches Docker through a dedicated, least-privilege channel; in production
  the worker itself runs unprivileged against a rootless/remote Docker or a socket-proxy
  that allow-lists only the API calls it needs (`create`, `start`, `wait`, `rm`, `logs`).

---

## 1.3 High-Level Architecture

```
                         ┌──────────────────────────────────────────────┐
                         │                   CLIENTS                     │
                         │   AI assistant · IDE · curl · SDK · browser   │
                         └───────────────┬──────────────────────────────┘
                                         │ HTTPS / WSS  (JWT or API key)
══════════ TRUST BOUNDARY 1 ═════════════╪══════════════════════════════════════════════
                                         ▼
        ┌────────────────────────────────────────────────────────────────┐
        │                   API GATEWAY  (Node.js / TS)                    │
        │  authn/z · sliding-window rate limit · zod validation · CORS     │
        │  /v1/execute  /v1/execute/async  /v1/jobs/:id  /v1/languages     │
        │  /v1/execute/stream (WS)  /v1/health  /v1/metrics                 │
        └───────┬───────────────────────────────────┬─────────────────────┘
                │ XADD job                           │ SUBSCRIBE stream:<id>
                ▼                                     ▲
        ┌──────────────────────────┐        ┌────────┴───────────────┐
        │  REDIS  (Streams + KV)   │        │   Redis Pub/Sub          │
        │  stream: sandbox:jobs    │        │   live stdout/stderr     │
        │  group:  workers         │        └────────▲─────────────────┘
        │  KV: job:<id> result:<id>│                 │ PUBLISH chunks
        └───────┬──────────────────┘                 │
                │ XREADGROUP / XACK / XPENDING        │
══════════ TRUST BOUNDARY 2 ═════════════════════════╪═══════════════════════════════════
                ▼                                     │
        ┌───────────────────────────────────────────────────────────────┐
        │              WORKER DAEMON  (Python / asyncio)   ×N             │
        │  consumer · Semaphore pool · result publisher · GC reaper      │
        │  ┌──────────────────────────────────────────────────────────┐  │
        │  │             SANDBOX EXECUTOR                              │  │
        │  │  image_registry → resource_limits → seccomp builder      │  │
        │  │  docker run [ --rm --network none --read-only             │  │
        │  │    --tmpfs /tmp:noexec --memory --cpus --pids-limit       │  │
        │  │    --cap-drop ALL --security-opt no-new-privileges        │  │
        │  │    --security-opt seccomp=<profile> --user nobody ]       │  │
══════════ TRUST BOUNDARY 3 ════════════════════════════════════════════════════════════
        │  │  ┌────────────────────────────────────────────────────┐  │  │
        │  │  │   EPHEMERAL SANDBOX CONTAINER  (UNTRUSTED CODE)     │  │  │  ← BOUNDARY 4
        │  │  │   runtime image · nobody · ro rootfs · no net      │  │  │
        │  │  └────────────────────────────────────────────────────┘  │  │
        │  └──────────────────────────────────────────────────────────┘  │
        └───────┬─────────────────────────────────────┬─────────────────┘
                │ store result                          │ artifacts
                ▼                                        ▼
        ┌──────────────────┐                   ┌──────────────────────┐
        │ Redis result:<id>│                   │  MinIO (S3 artifacts)│
        │ (hot, TTL 1h)    │                   │  stdout/files (cold) │
        └──────────────────┘                   └──────────────────────┘

        ┌───────────────────── OBSERVABILITY PLANE ───────────────────────┐
        │  Prometheus (scrape /metrics) → Grafana dashboards               │
        │  Loki (structured JSON logs)  ← API + Worker                     │
        │  OpenTelemetry traces: HTTP → queue → worker → container         │
        └─────────────────────────────────────────────────────────────────┘
```

**Data flows.** Solid downward arrows are the request path; the Pub/Sub arrow back up is
the live-output streaming path used by the WebSocket endpoint. The result store is written
by the worker and read by the API on poll. The observability plane is out-of-band.

**Isolation layers**, from least to most trusted code: container (Boundary 4) ⊂ worker
(Boundary 3) ⊂ queue/Redis (Boundary 2) ⊂ API (Boundary 1). Nothing from Boundary 4 ever
crosses back inward except bounded, validated result bytes.

---

## 1.4 Component Responsibilities

### 1. HTTP/WebSocket API Server  (Node.js + TypeScript)
- **Single responsibility:** terminate client connections, authenticate, authorise, rate-
  limit, validate, and translate requests into queue jobs / stream subscriptions. It does
  **no** code execution and never touches Docker.
- **Interface:** in — HTTP/WS requests (JSON, JWT/API key). out — `ExecutionResult` JSON,
  `202 + job_id`, WS event frames. Errors — RFC 7807 Problem Details.
- **Failure modes:** Redis down → `503` fail-fast (circuit breaker) for writes, rate-limit
  fails *open* (logged); MinIO down → results still served from Redis hot cache; worker
  backlog → `202` async or `429` with `Retry-After`.
- **Scaling:** **stateless**, horizontally scalable behind a load balancer; all state is in
  Redis. Sticky sessions only needed for a single WS connection (the WS subscribes to a
  Redis channel, so any replica can serve any stream).

### 2. Job Queue  (Redis Streams)
- **Single responsibility:** durable, ordered, at-least-once hand-off of jobs from API to
  workers, with consumer-group load balancing and stuck-job reclaim.
- **Interface:** in — `XADD sandbox:jobs * payload <json>`. out — `XREADGROUP GROUP workers`.
  ack — `XACK`. reclaim — `XAUTOCLAIM`/`XPENDING`. DLQ — `sandbox:jobs:dead`.
- **Failure modes:** consumer crash → entry stays pending, reclaimed after idle timeout;
  poison payload → moved to DLQ after `MAX_RETRIES`; Redis failover → AOF persistence
  (`appendfsync everysec`) bounds loss to ~1 s.
- **Scaling:** vertical (single Redis primary) with replicas for read/HA; consumer groups
  give horizontal worker scaling. For >50k jobs/s, shard streams by hash or migrate the
  durability tier to Kafka (see §1.9 migration path).

### 3. Worker Daemon  (Python + asyncio)
- **Single responsibility:** pull jobs, drive the sandbox lifecycle, stream + collect
  output, persist results, ack, and reap orphans. The host-trusted orchestrator.
- **Interface:** in — queue entries. out — Redis result/status writes, Pub/Sub chunks,
  MinIO artifacts, Prometheus metrics. Internal — calls `SandboxExecutor.execute()`.
- **Failure modes:** sandbox spawn fails → typed `SandboxError`, job `FAILED`; Docker
  daemon down → circuit breaker opens, jobs nack/retry with backoff; worker SIGKILL →
  in-flight entries reclaimed by peers; leaked container → GC reaper.
- **Scaling:** **stateless** (no local state survives a job); scale horizontally with
  `--scale worker=N`. Per-worker concurrency bounded by an `asyncio.Semaphore` sized to host
  CPU/RAM.

### 4. Sandbox Executor  (the isolation engine)
- **Single responsibility:** given `(language, code, stdin, limits)`, run it in a maximally-
  restricted ephemeral container and return an `ExecutionResult` — and *always* clean up.
- **Interface:** in — `ExecutionRequest` + `RuntimeConfig`. out — `ExecutionResult` (stdout,
  stderr, exit_code, wall/cpu time, memory, oom/timeout flags). Errors — `SandboxError`,
  `ImageNotFoundError`, `DockerUnavailableError`.
- **Failure modes:** image missing → clear typed error; timeout → SIGTERM→SIGKILL ladder,
  status `TIMEOUT`, partial output preserved; OOM → status `FAILED`, `oom_killed=true`;
  any exception → `finally` block removes container + temp dir.
- **Scaling:** stateless per call; throughput bounded by container start time (~150–400 ms
  cold) and host resources. Warm pools / pre-pulled images cut the tail.

### 5. Language Runtime Registry
- **Single responsibility:** the single source of truth mapping `language → {image digest,
  entrypoint argv, seccomp profile, resource defaults, file extension}`.
- **Interface:** `get(language) -> RuntimeConfig`; `list() -> RuntimeInfo[]`. Unknown
  language → `UnknownLanguageError` (surfaced as `400`).
- **Failure modes:** digest mismatch on verify → refuse to run (`ImageIntegrityError`).
- **Scaling:** static, in-memory, immutable; reloaded only on deploy.

### 6. Execution Result Store  (Redis + MinIO)
- **Single responsibility:** persist and serve execution results and artifacts.
- **Interface:** `put(job_id, result)` → Redis hot (`result:<id>`, TTL 1h) + MinIO cold
  (durable). `get(job_id)` → Redis first, MinIO fallback. Large stdout/files → MinIO with a
  pointer in Redis.
- **Failure modes:** Redis eviction → fall back to MinIO; MinIO down → degrade to hot-only
  with a logged warning.
- **Scaling:** Redis vertical + eviction policy; MinIO horizontal (erasure-coded).

### 7. Rate Limiter & Quota Manager
- **Single responsibility:** enforce per-IP and per-user request rates and concurrency
  quotas across tiers (anonymous / authenticated / premium).
- **Interface:** `check(key, tier) -> {allowed, remaining, retry_after}` via an atomic Redis
  Lua sliding-window. Quotas: requests/min, concurrent jobs, max code size.
- **Failure modes:** Redis unavailable → **fail open** (allow, log, alert) to preserve
  availability; documented trade-off.
- **Scaling:** stateless logic, state in Redis sorted sets; scales with Redis.

### 8. Monitoring & Alerting  (Prometheus + Grafana + Loki + OTel)
- **Single responsibility:** make the system observable — metrics, logs, traces — and alert
  on SLO breaches.
- **Interface:** Prometheus scrapes `/v1/metrics` (API) and `:9100` (worker); Loki ingests
  JSON logs; OTel exports spans. Grafana dashboards + alert rules.
- **Failure modes:** observability outage must **never** affect execution (out-of-band,
  best-effort, non-blocking exporters).
- **Scaling:** standard Prometheus/Loki horizontal patterns; metrics cardinality bounded
  (language × status, no per-job labels).

---

## 1.5 Execution Lifecycle

```
Client ──HTTP POST /v1/execute──────────────────────────────────────────────►
  │  (target ≤ 5 ms)  authn + rate-limit + zod validate
  ▼
API ──XADD sandbox:jobs──► Redis           (target ≤ 3 ms enqueue)
  │  sync path: SUBSCRIBE result + long-poll up to timeout
  ▼
Worker ──XREADGROUP (BLOCK 5s)──► picks job (target queue wait p95 ≤ 200 ms warm)
  │  Semaphore.acquire()
  ▼
Executor: verify image digest (≤ 1 ms cached) → mkdtemp + write code (≤ 5 ms)
  │
  ▼
docker create+start  (cold ≤ 400 ms, warm ≤ 150 ms)
  │  asyncio reads stdout/stderr → PUBLISH stream:<id> chunks (live)
  ▼
Execute under wall-clock timeout (asyncio.wait_for)
  │  on timeout: SIGTERM → wait 2 s → SIGKILL
  ▼
Collect exit code + cgroup metrics (memory.peak, cpu usage, oom flag) (≤ 10 ms)
  │
  ▼
resultStore.put → Redis result:<id> (+ MinIO if large)   (≤ 15 ms)
  │  PUBLISH stream:<id> {event:"done"} ;  XACK ;  container --rm ; rmtree temp
  ▼
API long-poll wakes → returns ExecutionResult   (sync end-to-end p95 ≤ 3 s)
  │  async path: client GETs /v1/jobs/:id later
  ▼
Client
```

**Job state machine:**

```
                 enqueue
        ┌──────────────────────► PENDING
        │                           │ worker XREADGROUP + Semaphore.acquire
        │                           ▼
        │                        RUNNING ──────────────┐
        │            normal exit  │   │   │  wall-clock │ user DELETE
        │       ┌────────────────┘   │   └───timeout──► │   /v1/jobs/:id
        │       ▼                    │                  ▼            ▼
        │   COMPLETED                │              TIMEOUT       KILLED
        │   (exit captured)          │ OOM / spawn err / nonzero-infra
        │                            ▼
        │                          FAILED
        │                            │
        └──── (DLQ after MAX_RETRIES on infra failure only) ───────┘

Terminal states: COMPLETED · FAILED · TIMEOUT · KILLED
Note: user code exiting non-zero is still COMPLETED (we captured its result);
FAILED is reserved for infrastructure/sandbox/OOM failures.
```

State transitions are written to `job:<id>` in Redis (`status`, `updated_at`) and emitted
as metrics. `PENDING→RUNNING` is set by the worker on dequeue; the API never sets
`RUNNING`. `RUNNING→*` is terminal and idempotent (compare-and-set guards against a
reclaimed-duplicate double-write).

---

## 1.6 Isolation Layers (Defence in Depth)

Each layer is independent; defeating the sandbox requires defeating several at once.

### Layer 1 — Process isolation (Linux namespaces)
Per container, fresh namespaces:
- **PID** — code sees only its own process tree; cannot signal host PIDs (`kill(1)` hits the
  container's own init, not the host). Caps the blast radius of `kill`/`ptrace`.
- **NET** — with `--network none`, only `lo`; no host interfaces, no route to the metadata
  service, no internal hosts.
- **MNT** — private mount table; cannot see host mounts; cannot `mount` new ones (also
  blocked by caps + seccomp).
- **UTS** — isolated hostname/domainname; prevents host-identity leakage and some uname-
  based exploits.
- **IPC** — no shared SysV/POSIX IPC with host or neighbours; blocks shared-memory side
  channels and IPC-based escapes.
- **USER** — container UIDs map to unprivileged host UIDs; "root in container" ≠ root on host.

### Layer 2 — Filesystem isolation
- **Read-only root** (`--read-only`): no writes to `/`, `/usr`, `/etc`, libraries — defeats
  binary-planting and config tampering.
- **Minimal writable tmpfs** `/tmp` `size=64m,noexec,nosuid,nodev`: a small scratch area
  that cannot hold executables and is wiped on exit.
- **No host paths**: the only mount is the per-job code dir, `:ro`. The host's `/`, Docker
  socket, secrets, and other tenants' data are simply not in the mount namespace.
- **Overlay construction**: the runtime image is an immutable lower layer; the read-only
  flag means there is no writable upper layer except the explicit tmpfs.

### Layer 3 — Network isolation
- **`--network none` by default**: no NIC, no DNS, no routes. The most reliable exfiltration
  defence — there is nothing to connect to.
- **Optional controlled egress**: for "install allowed packages" use-cases, attach a network
  whose only route is an **allow-list HTTP/HTTPS proxy**; everything else is dropped.
- **DNS sinkhole**: in egress mode, the resolver returns `NXDOMAIN`/`0.0.0.0` for any domain
  not on the allow-list, neutralising DNS-tunnelling and name-based exfiltration.

### Layer 4 — Syscall filtering (seccomp)
- **Default-deny** (`"defaultAction": "SCMP_ACT_ERRNO"`): unknown syscalls fail with `EPERM`
  rather than executing.
- **Per-runtime allow-list**: only the syscalls a given runtime legitimately needs are
  permitted (Python needs `futex`, `mmap`, `brk`, `epoll_*`; Bash needs far fewer).
- **Explicit denials** (never allow-listed for any runtime): `ptrace`, `process_vm_readv/
  writev`, `mount`, `umount2`, `pivot_root`, `chroot`, `unshare`, `setns`, `kexec_load`,
  `init_module`, `finit_module`, `delete_module`, `bpf`, `keyctl`, `add_key`, `request_key`,
  `perf_event_open`, `userfaultfd`, `setuid`/`setgid` (where not needed), and `clone`/`clone3`
  with namespace flags.
- **Custom JSON profiles** are stored per language under `runtimes/<lang>/seccomp-profile.json`
  and validated against the seccomp schema in CI.

### Layer 5 — Capability dropping
- **`--cap-drop ALL`**: start from zero Linux capabilities.
- **Add back only what is required, per runtime.** For these runtimes the answer is
  **nothing** — pure code execution needs no capability. We do **not** add `CAP_NET_BIND_
  SERVICE`, `CAP_SYS_ADMIN`, etc. If a future runtime genuinely needs one (rare), it is added
  to that runtime's config with a written justification and a security review. The default
  and the norm is the empty set.

### Layer 6 — Resource limits (cgroups v2)
- **CPU**: `--cpus 0.5` (per-runtime tuned) caps CPU bandwidth; a busy loop wastes only its
  own slice.
- **Memory**: `--memory 256m` with `--memory-swap 256m` (i.e. **swap disabled**); exceeding
  it triggers the **OOM killer** for that cgroup only, surfaced as `oom_killed=true`.
- **PIDs**: `--pids-limit 64` makes fork bombs hit a wall after 64 processes.
- **Disk I/O / size**: the tmpfs `size=` caps writable bytes; `--ulimit fsize` bounds single
  files; optional `--device-write-bps` for block I/O.
- **OOM config**: the cgroup OOM killer acts within the container; the host is shielded
  because the limit is enforced by the kernel before host memory is touched.
- **Wall-clock timeout**: enforced by the worker via `asyncio.wait_for`; on expiry, **SIGTERM
  → 2 s grace → SIGKILL** so well-behaved code can flush, misbehaving code is force-killed.

### Layer 7 — User namespace mapping
- Code runs as **`nobody` (uid 65534)** inside the container (`--user nobody`), never root.
- With **user namespaces** enabled on the daemon, container UID 65534 maps to a distinct,
  unprivileged *host* UID with no host rights — so even a container-root escape lands as a
  powerless host user.

### Layer 8 — Immutable infrastructure
- **Pre-built, pinned, signed images**: each runtime is built in CI, scanned (Trivy), signed
  (cosign), and referenced by **SHA-256 digest**, not a mutable tag.
- **No runtime package installation**: the sandbox has no network and a read-only rootfs, so
  `pip`/`npm install` cannot run. Dependencies are baked at build time from pinned mirrors.
- **Digest verification before execution**: the executor checks the local image's digest
  against the pinned value and refuses to run on mismatch (`ImageIntegrityError`).

---

## 1.7 API Design Specification

Base URL: `https://<host>/v1`. All bodies are JSON (`Content-Type: application/json`).
Auth: `Authorization: Bearer <JWT>` **or** `X-API-Key: <key>`. All errors are
[RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) Problem Details
(`Content-Type: application/problem+json`).

### `POST /v1/execute` — synchronous execution (≤ 30 s)
Runs code and blocks (long-poll) until it finishes or the timeout elapses.

- **Auth:** required (JWT or API key). **Rate limit:** per tier (anon 10/min, auth 60/min,
  premium 600/min).
- **Request** (`ExecutionRequest`):
  ```json
  {
    "language": "python",
    "code": "print('hello')",
    "stdin": "",
    "timeout_seconds": 10,
    "env_vars": { "GREETING": "hi" },
    "files": [ { "name": "data.txt", "content": "1,2,3" } ]
  }
  ```
- **Response** `200` (`ExecutionResult`):
  ```json
  {
    "job_id": "01J9Z8...",
    "status": "COMPLETED",
    "stdout": "hello\n",
    "stderr": "",
    "exit_code": 0,
    "wall_time_ms": 142,
    "cpu_time_ms": 31,
    "memory_bytes": 9437184,
    "oom_killed": false,
    "timed_out": false,
    "files": []
  }
  ```
- **Errors:** `400` validation (unknown language, code too large, bad timeout), `401`
  missing/invalid auth, `403` forbidden, `408` request timeout (job exceeded sync window),
  `429` rate limited (`Retry-After`), `503` Docker/Redis unavailable.

### `POST /v1/execute/async` — submit async, returns `job_id`
- **Auth/limits:** as above. **Response** `202`:
  `{ "job_id": "01J9Z8...", "status": "PENDING", "poll_url": "/v1/jobs/01J9Z8..." }`.
- **Errors:** `400`, `401`, `429`.

### `GET /v1/jobs/{job_id}` — poll status/result
- **Auth:** required; job is scoped to its owner.
- **Response** `200`: a `JobRecord` (status + result when terminal).
- **Errors:** `404` unknown job, `403` job belongs to another user.

### `DELETE /v1/jobs/{job_id}` — cancel
- Cancels a `PENDING` job (removed from stream) or kills a `RUNNING` job (container SIGKILL).
- **Response** `200` `{ "job_id": ..., "status": "KILLED" }`.
- **Errors:** `404` not found, `409` already terminal.

### `GET /v1/languages` — list runtimes
- **Auth:** optional. **Response** `200`:
  ```json
  { "languages": [ { "id": "python", "name": "Python", "version": "3.12",
                     "default_timeout_seconds": 10, "memory_mb": 256 } ] }
  ```

### `GET /v1/health` — liveness/readiness
- **Auth:** none. `200` `{ "status": "ok", "redis": "up", "docker": "up", "minio": "up" }`,
  `503` if a hard dependency is down.

### `GET /v1/metrics` — Prometheus
- **Auth:** none (network-restricted). `200` `text/plain; version=0.0.4` exposition format.

### `WS /v1/execute/stream` — real-time streaming
- **Upgrade:** auth validated on the HTTP upgrade (`Authorization`/`X-API-Key` header or
  `?token=`); rejected `401` otherwise.
- **Protocol:** client sends one `start` frame (an `ExecutionRequest`); server streams event
  frames and closes when done.
  - client → server: `{ "type": "start", "request": { …ExecutionRequest } }`
  - server → client:
    - `{ "type": "accepted", "job_id": "01J9Z8..." }`
    - `{ "type": "stdout", "data": "partial output" }`
    - `{ "type": "stderr", "data": "..." }`
    - `{ "type": "status", "status": "RUNNING" }`
    - `{ "type": "exit", "exit_code": 0, "status": "COMPLETED",
         "wall_time_ms": 142, "timed_out": false, "oom_killed": false }`
    - `{ "type": "error", "title": "...", "detail": "..." }`
- **Liveness:** ping/pong every 15 s; a connection that misses 2 pongs is closed.
- **Backpressure:** if the client's send buffer exceeds the high-water mark, the server
  sends `{ "type":"error", "detail":"backpressure: client too slow" }` and closes.

---

## 1.8 Data Models

TypeScript interfaces (mirrored 1:1 by Python dataclasses / Pydantic on the worker).

```ts
type Language = "python" | "javascript" | "typescript" | "java"
              | "go" | "ruby" | "rust" | "bash";

type JobStatus = "PENDING" | "RUNNING" | "COMPLETED"
               | "FAILED" | "TIMEOUT" | "KILLED";

interface InputFile { name: string; content: string; }   // name sanitised, no '..'/abs

interface ExecutionRequest {
  language: Language;
  code: string;                       // ≤ MAX_CODE_SIZE_BYTES
  stdin?: string;                     // ≤ MAX_STDIN_BYTES
  timeout_seconds?: number;           // 1 ≤ t ≤ MAX_TIMEOUT_SECONDS
  env_vars?: Record<string, string>;  // disallowed names stripped (PATH, LD_*, …)
  files?: InputFile[];                // additional read-only files mounted with the code
}

interface OutputFile { name: string; size_bytes: number; url: string; } // MinIO presigned

interface ExecutionResult {
  job_id: string;                     // ULID
  status: JobStatus;
  stdout: string;                     // ≤ output_max_bytes (truncated, flagged)
  stderr: string;
  exit_code: number | null;           // null if killed before exit
  wall_time_ms: number;
  cpu_time_ms: number;
  memory_bytes: number;               // cgroup memory.peak
  oom_killed: boolean;
  timed_out: boolean;
  truncated: boolean;                 // output hit output_max_bytes
  files: OutputFile[];
}

interface JobRecord {
  job_id: string;
  user_id: string;
  language: Language;
  status: JobStatus;
  request: ExecutionRequest;          // code may be redacted/omitted in audit reads
  result?: ExecutionResult;
  created_at: string;                 // RFC 3339
  updated_at: string;
  worker_id?: string;
  retries: number;
}

interface RuntimeConfig {
  id: Language;
  name: string;
  version: string;
  image: string;                      // repo:tag
  image_digest: string;               // sha256:… (verified before run)
  entrypoint: string[];               // argv; code path appended (never shell-interpolated)
  source_filename: string;            // e.g. "main.py"
  seccomp_profile: string;            // path to JSON profile
  limits: ResourceConfig;
}

interface ResourceConfig {
  memory_mb: number;
  cpu_quota: number;                  // 0.0–N CPUs
  timeout_seconds: number;
  pids_limit: number;
  disk_mb: number;                    // tmpfs size
  network_enabled: boolean;
  output_max_bytes: number;
}

type UserTier = "anonymous" | "authenticated" | "premium";

interface UserQuota {
  tier: UserTier;
  requests_per_minute: number;
  max_concurrent_jobs: number;
  max_code_size_bytes: number;
  max_timeout_seconds: number;
}
```

---

## 1.9 Technology Stack Justification

| Concern | Chosen | Over | Why / trade-off | Migration path |
|---|---|---|---|---|
| Isolation | Docker + seccomp + namespaces + cgroups (gVisor optional) | bare subprocess; full VMs (Firecracker); raw `runc` | Best balance of strong isolation, fast start (~150–400 ms), and operational familiarity. Trade-off: shared host kernel (mitigated by seccomp + optional gVisor). | Swap the container runtime to `runsc` (gVisor) or `kata`/Firecracker microVMs for a hardware/VM boundary; the executor only changes the `--runtime` flag. |
| API runtime | Node.js + TypeScript | Go; Python (FastAPI) | Excellent async I/O for many idle long-poll/WS connections; strong typing via TS strict; rich ecosystem (zod, ws). Trade-off: CPU-bound work is poor — but the API does none. | Re-implement the thin API in Go if connection counts demand it; contract is HTTP/JSON, no lock-in. |
| Worker orchestration | Python + asyncio | Go; Node | Best-in-class for *driving* Docker, building seccomp JSON, and gluing subprocess + cgroup metrics; readable security-critical code. Trade-off: GIL — fine, the work is I/O-bound subprocess management. | Port `executor.py` to Go for higher per-host concurrency; the queue contract is unchanged. |
| Queue | Redis Streams | Kafka; RabbitMQ; SQS | We already run Redis (cache, rate limit, pub/sub). Streams give consumer groups, at-least-once, `XPENDING` reclaim, and DLQs without new infra. Trade-off: single-primary throughput ceiling (~50k msg/s) and weaker multi-DC durability than Kafka. | Shard streams, or move durability to Kafka while keeping Redis for pub/sub streaming; the worker's consumer interface is abstracted. |
| Live streaming | Redis Pub/Sub | Kafka; direct WS from worker | Decouples worker from client; any API replica can serve any stream; trivially horizontal. Trade-off: at-most-once (a dropped subscriber misses chunks) — acceptable for *live* view; the durable result is always in the store. | Redis Streams per-job channel for replayable streaming if "catch up on missed output" is needed. |
| Hot results | Redis (TTL) | DB | Sub-ms reads for polling; natural TTL eviction. Trade-off: volatile. | — |
| Cold artifacts | MinIO (S3 API) | local disk; cloud S3 | S3-compatible, self-hostable, durable (erasure coding) for large stdout/files; presigned URLs offload bytes from the API. Trade-off: extra component. | Point the S3 client at AWS S3/GCS with no code change. |
| Monitoring | Prometheus + Grafana + Loki | ELK; Datadog | Pull metrics + label model fit; Loki shares Grafana and is cheap for JSON logs; all self-hostable. Trade-off: Loki's query power < Elasticsearch. | OTLP export to any vendor; instrumentation is OpenTelemetry-native. |
| Tracing | OpenTelemetry | vendor SDKs | Vendor-neutral, propagates trace context HTTP→queue→worker. | Swap the OTLP endpoint. |
| Auth | JWT + hashed API keys | sessions; OAuth gateway | Stateless, scales horizontally, no per-request DB hit; API keys for machine clients. Trade-off: revocation needs a denylist (short TTLs mitigate). | Front with an OAuth2/OIDC gateway; the API only needs a verified principal. |
| CI/CD | GitHub Actions + BuildKit | Jenkins; GitLab CI | Hosted, declarative, BuildKit cache + multi-arch; cosign/Trivy integrate cleanly. | Port YAML to any runner; steps are plain CLI. |

---

## 1.10 Operational Runbook (summary — full version in `RUNBOOK.md`)

### Initial deployment
- **Local dev:** `cp .env.example .env` → `make build` → `make dev` (compose: api, worker,
  redis, minio, prometheus, grafana, loki) → `make test`.
- **Staging:** same compose with `docker-compose.prod.yml` overrides, real secrets from the
  secret manager, `--scale worker=3`, image digests pinned, TLS terminated at the LB.
- **Production:** Kubernetes (or compose on a hardened host): API as a Deployment behind an
  Ingress/LB; workers as a Deployment with node anti-affinity and a dedicated, hardened
  Docker/gVisor node pool; Redis with AOF + replica; MinIO erasure-coded; Prometheus/Grafana/
  Loki stack; cosign-verified images only.

### Add a new language runtime (checklist)
1. `runtimes/<lang>/Dockerfile` from a pinned minimal base, drop dangerous binaries, `USER nobody`.
2. `runtimes/<lang>/seccomp-profile.json` (start from the nearest profile, add only needed syscalls).
3. Register in `worker/sandbox/image_registry.py` (image, digest, entrypoint, filename, profile).
4. Add `ResourceConfig` defaults in `worker/sandbox/resource_limits.py`.
5. `make build` + `make security-scan` (Trivy must pass; pin all versions).
6. Add language smoke + isolation tests.
7. Update the languages table in `docs/API.md` and `README.md`.

### Incident response (common failures)
- **Worker crash:** entries it held go pending; peers `XAUTOCLAIM` after the idle timeout and
  re-run them; the GC reaper removes any leaked container. Restart the worker; confirm
  `sandbox_queue_depth` drains.
- **Queue backlog:** scale workers (`--scale worker=N`); check Docker daemon health and image
  pull latency; the API sheds load via `429`/async; verify the circuit breaker isn't open.
- **OOM-kill storm:** a spike in `sandbox_oom_kills_total` usually means a payload pattern or a
  too-tight limit. Inspect by language label; temporarily raise that runtime's `memory_mb` or
  tighten validation; never raise the *host* memory headroom below the reaper's safety margin.
- **Docker daemon down:** the circuit breaker opens, the API fails fast with `503`, jobs stay
  pending (no data loss). Recover the daemon; the breaker half-opens and drains the backlog.

### Scaling strategy
- **Vertical first** on workers (more CPU/RAM ⇒ higher `WORKER_CONCURRENCY`), then **horizontal**
  (more worker replicas). API scales horizontally and is almost never the bottleneck.
- Watch `sandbox_container_startup_seconds` (pre-pull images, warm pools) and queue depth (the
  scaling signal). Keep host memory headroom ≥ `concurrency × memory_mb × 1.3`.

### Backup & disaster recovery
- **Redis:** AOF `everysec` + periodic RDB snapshots to object storage; replica for HA. RPO ≈ 1 s.
- **MinIO:** erasure-coded + cross-site replication for artifacts; lifecycle expiry for old results.
- **Stateless tiers** (API, worker) need no backup — redeploy from pinned images.
- **DR drill:** quarterly restore of Redis + MinIO into a clean environment; verify `make test-e2e`
  passes against it.
