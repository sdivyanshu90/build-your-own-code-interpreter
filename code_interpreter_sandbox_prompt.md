# Expert Prompt: Build Your Own Code Interpreter Sandbox
### Authored by an Expert Prompt Engineer at Anthropic

---

## ════════════════════════════════════════════════
## MASTER PROMPT — COPY EVERYTHING BELOW THIS LINE
## ════════════════════════════════════════════════

---

You are a **Principal Software Engineer and Security Architect** with deep expertise in:
- Container isolation, OS-level sandboxing (seccomp, namespaces, cgroups)
- Polyglot runtime execution engines
- Distributed systems and REST/WebSocket API design
- Software security hardening and threat modelling
- Test-driven development and observability-first engineering

Your task is to design, document, and implement a **production-grade, self-hosted Code Interpreter Sandbox** from end to end. This is not a toy prototype — it is an enterprise-ready system capable of safely executing untrusted code submitted by end users, identical in capability to systems used in AI coding assistants, online judges, and notebook environments.

Do **not** produce placeholders, stubs, or TODO comments. Every file, function, and test must be complete and runnable.

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PART 1 — ARCHITECTURE & SYSTEM DESIGN DOCUMENT
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Produce a **complete Architecture & Design Document** (`docs/ARCHITECTURE.md`) covering every section below. Each section must be thorough, specific, and technically precise — not vague or hand-wavy.

### 1.1 Executive Summary
Write a one-page technical overview that answers:
- What problem does this system solve and for whom?
- What are the top three non-negotiable technical constraints (security, latency, resource isolation)?
- How does this design differ from naive approaches (e.g., bare subprocess execution) and why?

### 1.2 Threat Model
Enumerate every attack surface with severity (Critical / High / Medium / Low):

| Threat | Attack Vector | Impact | Mitigation |
|--------|---------------|--------|------------|
| Arbitrary command execution | Malicious user code | Critical | … |
| Host filesystem escape | Path traversal in code | Critical | … |
| Resource exhaustion (fork bomb, infinite loop) | Untrusted code | High | … |
| Network exfiltration | Code opens sockets | High | … |
| Side-channel via shared kernel | Co-tenancy | Medium | … |
| Privilege escalation via kernel exploits | Malicious syscalls | Critical | … |
| Dependency confusion / supply chain attack | Package installation | High | … |
| Sandbox escape via /proc, /sys, /dev | Filesystem access | Critical | … |

Add any additional threats you identify. For every Critical threat, provide a defence-in-depth strategy with at least two independent mitigating layers.

### 1.3 High-Level Architecture Diagram
Draw an ASCII architecture diagram showing all components: API Gateway, Job Queue, Worker Pool, Sandbox Executor, Ephemeral Container Layer, Result Store, Monitoring Stack. Show data flows, trust boundaries, and isolation layers.

### 1.4 Component Responsibilities
For each component, document:
- Purpose and single responsibility
- Interface contracts (inputs, outputs, error conditions)
- Failure modes and recovery strategy
- Scaling characteristics (stateless vs. stateful, horizontal vs. vertical)

Components to cover:
1. **HTTP/WebSocket API Server** — accepts code submissions, streams output
2. **Job Queue** (Redis Streams or RabbitMQ) — decoupled async execution
3. **Worker Daemon** — pulls jobs, manages sandbox lifecycle
4. **Sandbox Executor** — the core isolation engine (Docker + seccomp + namespaces)
5. **Language Runtime Registry** — maps language → Docker image + entrypoint
6. **Execution Result Store** (Redis + object storage) — persists stdout/stderr/artifacts
7. **Rate Limiter & Quota Manager** — per-user/per-IP limits
8. **Monitoring & Alerting** (Prometheus + Grafana + structured logging)

### 1.5 Execution Lifecycle
Document the complete lifecycle of a single code execution request from HTTP request to final response, with timing targets at each step:

```
Client → API Server → Validate → Enqueue → Worker picks up →
Spawn container → Inject code → Execute → Stream output →
Collect result → Store → Return response → Cleanup
```

Include state machine diagrams for job states: `PENDING → RUNNING → COMPLETED | FAILED | TIMEOUT | KILLED`.

### 1.6 Isolation Layers (Defence in Depth)
Explain each isolation layer in detail:

**Layer 1 — Process Isolation**
- Linux namespaces used: PID, NET, MNT, UTS, IPC, USER
- How each namespace prevents specific escape vectors

**Layer 2 — Filesystem Isolation**
- Read-only root filesystem
- Minimal tmpfs `/tmp` with size limit
- No access to host paths; overlay filesystem construction

**Layer 3 — Network Isolation**
- No external network by default (--network none)
- Optional controlled egress via dedicated proxy with allowlist
- DNS sinkholes for blocked domains

**Layer 4 — Syscall Filtering (seccomp)**
- Default-deny seccomp profile
- Explicit allowlist of safe syscalls for each language runtime
- Custom seccomp JSON profiles per language

**Layer 5 — Capability Dropping**
- Drop ALL Linux capabilities
- Add back only what each runtime requires (explain which and why)

**Layer 6 — Resource Limits (cgroups v2)**
- CPU quota (e.g., 0.5 CPU), memory limit, PID limit, disk I/O limit
- OOM killer configuration
- Wall-clock timeout via SIGKILL sequence

**Layer 7 — User Namespace Mapping**
- Execute as non-root inside container (uid 65534 / nobody)
- User namespace mapping to prevent host privilege escalation

**Layer 8 — Immutable Infrastructure**
- Pre-built, pinned, signed runtime images
- No package installation at runtime
- Image digest verification before execution

### 1.7 API Design Specification
Define the full REST + WebSocket API:

**REST Endpoints:**

```
POST   /v1/execute              — submit a synchronous execution (≤30s)
POST   /v1/execute/async        — submit async job, returns job_id
GET    /v1/jobs/{job_id}        — poll job status and result
DELETE /v1/jobs/{job_id}        — cancel a running job
GET    /v1/languages            — list supported runtimes
GET    /v1/health               — liveness probe
GET    /v1/metrics              — Prometheus metrics endpoint
```

**WebSocket Endpoint:**
```
WS     /v1/execute/stream       — real-time stdout/stderr streaming
```

For every endpoint document: request schema, response schema, error codes, rate limits, authentication requirements, and example request/response pairs.

### 1.8 Data Models
Define all core data structures as language-agnostic schemas (JSON Schema or TypeScript interfaces):
- `ExecutionRequest` (code, language, stdin, timeout, env_vars, files)
- `ExecutionResult` (job_id, status, stdout, stderr, exit_code, wall_time_ms, cpu_time_ms, memory_bytes, files)
- `JobRecord` (all execution metadata for audit)
- `RuntimeConfig` (per-language Docker image, entrypoint, seccomp profile, resource limits)
- `UserQuota` (requests/hour, concurrent jobs, max code size)

### 1.9 Technology Stack Justification
For each technology chosen, explain:
- Why this tool over alternatives (e.g., gVisor vs. seccomp+namespaces; Redis Streams vs. Kafka)
- Trade-offs accepted
- Migration path if requirements change

Recommended stack to document and justify:
- **Runtime**: Node.js (API) + Python (worker orchestration)
- **Sandbox**: Docker with custom seccomp profiles
- **Queue**: Redis Streams
- **Result cache**: Redis + MinIO
- **Monitoring**: Prometheus + Grafana + Loki
- **Auth**: JWT + API keys
- **CI/CD**: GitHub Actions + Docker BuildKit

### 1.10 Operational Runbook
Document:
- Initial deployment steps (local dev, staging, production)
- How to add a new language runtime (step-by-step checklist)
- Incident response for common failures (worker crash, queue backlog, OOM kill storm)
- Scaling strategy under load
- Backup and disaster recovery

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PART 2 — PRODUCTION-READY CODE IMPLEMENTATION
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Implement the entire system. Every file must be complete — no stubs, no TODOs, no ellipses.

### 2.1 Project Structure

Generate this exact directory tree, with every file fully implemented:

```
code-sandbox/
├── README.md                          # Full setup & usage guide
├── docker-compose.yml                 # Full local dev stack
├── docker-compose.prod.yml            # Production overrides
├── .env.example                       # All env vars documented
├── Makefile                           # Dev, test, lint, deploy targets
│
├── api/                               # Node.js/TypeScript API Server
│   ├── package.json
│   ├── tsconfig.json
│   ├── src/
│   │   ├── index.ts                   # Server bootstrap
│   │   ├── config.ts                  # Typed config from env
│   │   ├── routes/
│   │   │   ├── execute.ts             # POST /v1/execute
│   │   │   ├── jobs.ts                # GET/DELETE /v1/jobs/:id
│   │   │   ├── languages.ts           # GET /v1/languages
│   │   │   └── health.ts              # GET /v1/health
│   │   ├── middleware/
│   │   │   ├── auth.ts                # JWT + API key validation
│   │   │   ├── rateLimiter.ts         # Per-user sliding window rate limit
│   │   │   ├── validator.ts           # Zod schema validation
│   │   │   └── errorHandler.ts        # Centralized error handling
│   │   ├── services/
│   │   │   ├── jobQueue.ts            # Redis Streams producer
│   │   │   ├── resultStore.ts         # Redis + MinIO result fetching
│   │   │   └── websocket.ts           # WS streaming handler
│   │   └── types/
│   │       └── index.ts               # All shared TypeScript types
│   └── Dockerfile
│
├── worker/                            # Python Worker Daemon
│   ├── requirements.txt
│   ├── worker.py                      # Main worker loop
│   ├── sandbox/
│   │   ├── __init__.py
│   │   ├── executor.py                # Core Docker sandbox executor
│   │   ├── seccomp.py                 # Seccomp profile builder
│   │   ├── resource_limits.py         # cgroup/Docker resource config
│   │   ├── image_registry.py          # Language → image mapping
│   │   └── cleanup.py                 # Container cleanup & GC
│   ├── queue/
│   │   ├── __init__.py
│   │   ├── consumer.py                # Redis Streams consumer
│   │   └── publisher.py               # Result publishing
│   ├── storage/
│   │   ├── __init__.py
│   │   └── result_store.py            # Redis + MinIO integration
│   └── Dockerfile
│
├── runtimes/                          # Runtime Docker images
│   ├── python/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   ├── javascript/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   ├── typescript/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   ├── java/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   ├── go/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   ├── ruby/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   ├── rust/
│   │   ├── Dockerfile
│   │   └── seccomp-profile.json
│   └── bash/
│       ├── Dockerfile
│       └── seccomp-profile.json
│
├── monitoring/
│   ├── prometheus.yml
│   ├── grafana/
│   │   └── dashboards/
│   │       └── sandbox-overview.json  # Full Grafana dashboard
│   └── loki/
│       └── config.yml
│
├── docs/
│   ├── ARCHITECTURE.md                # From Part 1
│   ├── API.md                         # Full API reference
│   ├── SECURITY.md                    # Security hardening guide
│   ├── ADDING_LANGUAGE.md             # How to add a new runtime
│   └── RUNBOOK.md                     # Operational runbook
│
└── tests/
    ├── unit/                          # From Part 3
    ├── integration/
    └── e2e/
```

### 2.2 Core Implementation Requirements

#### API Server (`api/src/`)

**`config.ts`** — Must use `zod` to parse and validate all environment variables at startup with descriptive errors. Include: `PORT`, `REDIS_URL`, `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `JWT_SECRET`, `MAX_CODE_SIZE_BYTES`, `DEFAULT_TIMEOUT_SECONDS`, `MAX_TIMEOUT_SECONDS`, `RATE_LIMIT_REQUESTS_PER_MINUTE`, `WORKER_CONCURRENCY`.

**`routes/execute.ts`** — Must handle:
- Request validation (language exists, code size ≤ limit, timeout ≤ max)
- Synchronous execution path with long-polling (≤30s)
- Job ID generation using `ulid` (sortable, URL-safe)
- Full OpenAPI-annotated JSDoc
- Structured error responses following RFC 7807 Problem Details

**`services/jobQueue.ts`** — Must implement:
- Redis Streams `XADD` with job payload serialization
- Consumer group creation with `XGROUP CREATE MKSTREAM`
- Dead-letter handling for jobs that exceed retry limit
- Graceful shutdown with in-flight job tracking

**`middleware/rateLimiter.ts`** — Must implement:
- Sliding window rate limiting (not fixed window) using Redis sorted sets
- Separate limits for anonymous, authenticated, and premium tiers
- `Retry-After` header on 429 responses
- IP-based and user-ID-based limiting with combined enforcement

**`services/websocket.ts`** — Must implement:
- WebSocket upgrade with auth validation
- Pub/Sub via Redis for worker → client streaming
- Heartbeat/ping-pong to detect dead connections
- Backpressure handling: buffer overflow → client disconnect with error message

#### Worker Daemon (`worker/`)

**`sandbox/executor.py`** — This is the most critical file. It must:

```python
# The execute() method must:
# 1. Pull/verify the runtime image by digest (pinned SHA256)
# 2. Create a unique temp directory for code injection
# 3. Write user code to a temp file (never shell-interpolated)
# 4. Construct docker run command with ALL of these flags:
#    --rm                           # auto-remove on exit
#    --network none                 # no network access
#    --read-only                    # read-only root FS
#    --tmpfs /tmp:size=64m,noexec   # limited writable tmp, no exec
#    --memory 256m                  # memory limit
#    --memory-swap 256m             # disable swap
#    --cpus 0.5                     # CPU limit
#    --pids-limit 64                # prevent fork bombs
#    --ulimit nofile=64:64          # file descriptor limit
#    --ulimit nproc=64:64           # process limit
#    --user nobody                  # non-root user
#    --cap-drop ALL                 # drop all capabilities
#    --security-opt no-new-privileges  # prevent privilege escalation
#    --security-opt seccomp=<profile>  # custom seccomp profile
#    -v <code_dir>:/sandbox:ro      # mount code read-only
#    -e SANDBOX=1                   # env flag
#    --stop-timeout 2               # fast SIGKILL after SIGTERM
# 5. Execute with asyncio subprocess, capture stdout/stderr in real-time
# 6. Enforce wall-clock timeout with asyncio.wait_for()
# 7. On timeout: send SIGTERM, wait 2s, send SIGKILL
# 8. Always clean up temp directory in finally block
# 9. Return ExecutionResult with all metrics
```

**`sandbox/seccomp.py`** — Must generate language-specific seccomp profiles that:
- Default deny all syscalls (`"defaultAction": "SCMP_ACT_ERRNO"`)
- Allowlist only syscalls required for each language runtime
- Block dangerous syscalls explicitly: `ptrace`, `process_vm_readv`, `process_vm_writev`, `mount`, `unshare`, `setuid`, `setgid`, `chroot`, `pivot_root`, `clone` (with restrictions), `socket` (configurable), `execve` (restricted to runtime only)
- Include separate profiles for: Python, Node.js, Java (JVM quirks), Go (compiled binaries), Ruby, Rust, Bash

**`sandbox/resource_limits.py`** — Must define per-language `ResourceConfig` dataclasses:
```python
@dataclass
class ResourceConfig:
    memory_mb: int           # RAM limit
    cpu_quota: float         # CPU fraction (0.0–1.0)
    timeout_seconds: int     # Wall clock timeout
    pids_limit: int          # Max PIDs (fork bomb prevention)
    disk_mb: int             # Max /tmp size
    network_enabled: bool    # Network access flag
    output_max_bytes: int    # Max stdout+stderr bytes
```
Provide tuned defaults for each language (Java needs more memory; Bash needs stricter pids_limit).

**`queue/consumer.py`** — Must implement:
- `XREADGROUP` with `COUNT` and `BLOCK` for efficient polling
- Concurrent job execution using `asyncio.Semaphore` for worker pool limits
- `XACK` only after successful result storage (at-least-once delivery)
- `XPENDING` claim of stale in-flight jobs after 60 seconds (dead worker recovery)
- Graceful shutdown: stop accepting new jobs, wait for in-flight jobs to complete

#### Runtime Dockerfiles (`runtimes/`)

Each runtime Dockerfile must:
1. Start from an official minimal base (`python:3.12-slim`, `node:20-alpine`, etc.)
2. Create a dedicated `sandbox` group (gid 65534) and `nobody` user
3. Remove or replace dangerous binaries: `sudo`, `su`, `chmod`, `chown`, `passwd`, `useradd`, `nc`, `wget`, `curl` (except where needed for package install)
4. Set `USER nobody` as the final instruction
5. Verify the image has no critical CVEs using a build-time `trivy` scan step
6. Pin all package versions

#### Security Profiles

**`runtimes/python/seccomp-profile.json`** — Complete, valid seccomp JSON for CPython, covering all syscalls needed for normal Python execution (including `futex`, `mmap`, `brk`, etc.) while blocking escape vectors.

**`runtimes/bash/seccomp-profile.json`** — Most restrictive profile: block `socket`, `connect`, `execve` for new binaries not in PATH, `ptrace`, `mount`.

### 2.3 docker-compose.yml

Must define all services with health checks, dependency ordering, resource limits, and named volumes:
- `api` — the Node.js API server
- `worker` — Python worker (scalable with `--scale worker=N`)
- `redis` — Redis 7 with persistence enabled
- `minio` — MinIO for artifact storage, with bucket creation in startup
- `prometheus` — metrics scraping
- `grafana` — dashboards with provisioned datasource
- `loki` — log aggregation

### 2.4 Makefile Targets

Implement all of:
```makefile
make dev          # Start full local stack with hot reload
make build        # Build all Docker images
make test         # Run full test suite (unit + integration + e2e)
make test-unit    # Unit tests only
make test-integration  # Integration tests with real Redis/Docker
make test-e2e     # End-to-end API tests
make lint         # Lint all code (eslint + ruff + mypy)
make security-scan  # Trivy scan all images
make clean        # Remove containers, volumes, temp files
make push-runtimes  # Tag and push runtime images to registry
make load-test    # Run k6 load test scenario
```

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PART 3 — COMPLETE TEST SUITE
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The test suite must achieve ≥90% line coverage and include unit, integration, and end-to-end tests. All tests must be runnable with `make test`.

### 3.1 Unit Tests (`tests/unit/`)

#### `tests/unit/test_executor.py`

Test the sandbox executor in isolation using Docker mock fixtures. Cover every code path:

```python
# Required test cases (implement all):

class TestSandboxExecutorUnit:

    # --- Happy path ---
    def test_python_hello_world_returns_stdout()
    def test_javascript_console_log_returns_output()
    def test_stdin_is_piped_to_program()
    def test_multi_file_execution_with_imports()
    def test_env_vars_are_injected_correctly()
    def test_exit_code_zero_on_success()
    def test_nonzero_exit_code_captured_correctly()
    def test_stderr_captured_separately_from_stdout()
    def test_wall_time_ms_is_measured_accurately()
    def test_memory_bytes_reported_correctly()

    # --- Security: ensure isolation is enforced ---
    def test_network_access_is_blocked()              # code tries socket.connect()
    def test_cannot_write_outside_tmp()               # code tries open('/etc/evil','w')
    def test_cannot_read_host_filesystem()            # code tries open('/etc/passwd')
    def test_fork_bomb_is_killed_within_timeout()     # code does while True: os.fork()
    def test_infinite_loop_times_out_correctly()      # code does while True: pass
    def test_subprocess_spawning_is_blocked_in_bash() # bash tries nc/wget
    def test_kernel_module_load_blocked()             # code tries modprobe
    def test_ptrace_syscall_is_denied()               # code tries ctypes ptrace
    def test_setuid_is_denied()                       # code tries os.setuid(0)
    def test_mount_syscall_is_denied()                # code tries mount()
    def test_cannot_kill_pid_1()                      # code tries os.kill(1, 9)
    def test_privilege_escalation_via_suid_blocked()

    # --- Resource limits ---
    def test_oom_kill_on_memory_excess()              # allocate 512MB in a 256MB limit
    def test_cpu_quota_enforced()                     # verify CPU time ≤ allowed
    def test_output_truncated_at_max_bytes()          # code prints 100MB of data
    def test_pids_limit_prevents_fork_bomb()
    def test_disk_write_limit_enforced()              # code writes until tmpfs full

    # --- Error handling ---
    def test_syntax_error_returns_stderr_not_exception()
    def test_runtime_exception_returns_stderr_and_nonzero_exit()
    def test_missing_image_raises_clear_error()
    def test_docker_daemon_unavailable_raises_sandbox_error()
    def test_code_injection_via_filename_is_prevented()  # filename = "; rm -rf /"
    def test_cleanup_always_runs_even_on_exception()
    def test_container_is_always_removed_after_execution()
    def test_temp_directory_is_always_deleted()

    # --- Timeout ---
    def test_timeout_sends_sigterm_then_sigkill()
    def test_timeout_result_has_timed_out_status()
    def test_partial_output_returned_on_timeout()
    def test_custom_timeout_overrides_default()
    def test_timeout_zero_rejected_at_validation()
```

#### `tests/unit/test_seccomp.py`

```python
class TestSeccompProfiles:
    def test_default_deny_present_in_all_profiles()
    def test_python_profile_allows_required_syscalls()
    def test_python_profile_denies_ptrace()
    def test_python_profile_denies_socket_when_network_disabled()
    def test_bash_profile_is_most_restrictive()
    def test_java_profile_allows_clone_with_restrictions()
    def test_all_profiles_are_valid_json()
    def test_all_profiles_pass_seccomp_schema_validation()
    def test_dangerous_syscalls_denied_in_all_profiles()
        # ptrace, process_vm_readv, kexec_load, init_module, finit_module,
        # mount, umount2, pivot_root, chroot, unshare, setuid, setgid
```

#### `tests/unit/test_rate_limiter.ts`

```typescript
describe('SlidingWindowRateLimiter', () => {
  // --- Core correctness ---
  it('allows requests within the limit')
  it('blocks request that exceeds limit')
  it('resets correctly after window expires')
  it('sliding window is not fixed window (old events expire)')
  it('different users have independent limits')
  it('returns correct Retry-After value when blocked')
  it('authenticated users get higher limits than anonymous')

  // --- Redis failure handling ---
  it('fails open on Redis timeout (logs warning, allows request)')
  it('recovers after Redis reconnects')

  // --- Edge cases ---
  it('handles concurrent requests atomically via Lua script')
  it('zero requests allowed returns 429 immediately')
  it('very large window size does not overflow sorted set')
})
```

#### `tests/unit/test_job_queue.ts`

```typescript
describe('JobQueue', () => {
  it('enqueues job with correct stream key and payload')
  it('consumer group is created if not exists')
  it('job ID is a valid ULID')
  it('dead letter queue receives jobs after max retries')
  it('stale pending jobs are claimed after timeout')
  it('graceful shutdown waits for in-flight jobs')
  it('serialization round-trip preserves all fields')
  it('large code payload is handled correctly')
  it('invalid language is rejected before enqueueing')
})
```

#### `tests/unit/test_validator.ts`

```typescript
describe('ExecutionRequestValidator', () => {
  it('accepts valid Python request')
  it('rejects code exceeding MAX_CODE_SIZE_BYTES')
  it('rejects unknown language')
  it('rejects timeout exceeding MAX_TIMEOUT_SECONDS')
  it('rejects negative timeout')
  it('rejects empty code string')
  it('rejects null code')
  it('strips disallowed env var names (PATH, LD_PRELOAD, etc.)')
  it('rejects env var values exceeding max length')
  it('accepts valid stdin bytes')
  it('rejects stdin exceeding MAX_STDIN_BYTES')
  it('returns RFC 7807 Problem Details on invalid input')
})
```

#### `tests/unit/test_resource_limits.py`

```python
class TestResourceLimits:
    def test_python_defaults_are_reasonable()
    def test_java_gets_higher_memory_than_python()
    def test_bash_gets_lower_pids_limit()
    def test_custom_timeout_clamped_to_max()
    def test_network_disabled_by_default()
    def test_resource_config_serializes_to_docker_flags()
    def test_output_limit_prevents_log_flooding()
```

### 3.2 Integration Tests (`tests/integration/`)

Integration tests run against a real local Docker daemon and real Redis instance (via docker-compose). They test the actual system behaviour end-to-end within the backend, without going through the HTTP API.

#### `tests/integration/test_executor_real_docker.py`

```python
# Uses pytest fixtures: running Redis, Docker daemon, real runtime images

@pytest.mark.integration
class TestRealDockerExecution:

    # --- Language smoke tests ---
    def test_python_print_hello_world()
    def test_python_3_12_f_strings_work()
    def test_javascript_node_20_async_await()
    def test_typescript_compiled_and_run()
    def test_java_21_records_work()
    def test_go_1_22_generics_work()
    def test_ruby_3_3_hello_world()
    def test_rust_hello_world_compiled()
    def test_bash_echo_hello_world()

    # --- Real isolation verification ---
    def test_python_cannot_connect_to_internet()         # tries requests.get(google.com)
    def test_python_cannot_read_host_proc()              # tries open('/proc/1/cmdline')
    def test_python_fork_bomb_killed_within_5_seconds()  # real fork bomb
    def test_javascript_infinite_loop_times_out()        # real timeout enforcement
    def test_bash_cannot_call_wget()                     # wget should not exist
    def test_java_cannot_open_socket()                   # real network isolation

    # --- Real resource enforcement ---
    def test_python_oom_killed_correctly()               # allocates 500MB in 256MB limit
    def test_container_is_not_present_after_execution()  # docker ps verifies cleanup
    def test_temp_files_cleaned_up_on_host()             # check /tmp is clean

    # --- Queue + worker integration ---
    def test_job_submitted_to_redis_stream_and_consumed()
    def test_result_stored_in_redis_and_retrievable()
    def test_multiple_concurrent_jobs_execute_independently()
    def test_worker_recovers_stale_job_from_another_dead_worker()
    def test_job_cancellation_kills_running_container()
```

#### `tests/integration/test_api_server.ts`

```typescript
// Uses supertest against a running API server with real Redis

describe('API Integration Tests', () => {

  describe('POST /v1/execute', () => {
    it('executes Python synchronously and returns result')
    it('executes JavaScript and returns result')
    it('returns 400 for unknown language')
    it('returns 400 for oversized code')
    it('returns 408 when execution times out')
    it('returns 429 when rate limit exceeded')
    it('returns 401 without auth token')
    it('returns 403 with invalid token')
    it('streams partial output on timeout')
  })

  describe('POST /v1/execute/async', () => {
    it('returns 202 Accepted with job_id immediately')
    it('job_id is a valid ULID')
    it('result is available via GET /v1/jobs/:id after completion')
  })

  describe('GET /v1/jobs/:id', () => {
    it('returns PENDING status for newly created job')
    it('returns RUNNING status for executing job')
    it('returns COMPLETED with full result')
    it('returns FAILED with error details')
    it('returns TIMEOUT status for timed-out job')
    it('returns 404 for unknown job_id')
    it('returns 403 if job belongs to different user')
  })

  describe('DELETE /v1/jobs/:id', () => {
    it('cancels a PENDING job')
    it('kills a RUNNING job and container')
    it('returns 409 if job already completed')
    it('returns 404 if job not found')
  })

  describe('WS /v1/execute/stream', () => {
    it('streams stdout in real-time as program prints')
    it('streams stderr separately')
    it('sends exit_code event on completion')
    it('closes connection after execution ends')
    it('rejects unauthenticated WebSocket upgrade')
    it('handles backpressure without crashing server')
  })

  describe('GET /v1/languages', () => {
    it('returns list of all supported runtimes')
    it('each language has version, name, and id fields')
  })
})
```

### 3.3 End-to-End Tests (`tests/e2e/`)

E2E tests run against the full docker-compose stack and simulate real user scenarios.

#### `tests/e2e/test_user_scenarios.ts`

```typescript
// Full user journey tests using axios against the live API

describe('E2E: Real User Scenarios', () => {

  it('User runs Python fibonacci program and gets correct output', async () => {
    // Submit code, poll until COMPLETED, verify stdout === "55"
  })

  it('User runs a program that reads stdin', async () => {
    // Submit with stdin: "42", verify code reads and prints it
  })

  it('User submits malicious code — tries to read /etc/passwd', async () => {
    // Verify exit code non-zero, no passwd content in output
  })

  it('User submits a fork bomb — system remains responsive', async () => {
    // Submit fork bomb, verify it times out AND next request still succeeds
  })

  it('User submits code that allocates 1GB RAM', async () => {
    // Verify OOM kill, status=FAILED, system still responsive
  })

  it('Rate limiting: user is blocked after 60 requests/minute', async () => {
    // Fire 61 requests, assert 60 succeed and 61st returns 429
  })

  it('Concurrent execution: 10 users submit code simultaneously', async () => {
    // All 10 complete correctly and independently
  })

  it('WebSocket streaming: user receives output line-by-line', async () => {
    // Connect WS, submit code that sleeps and prints, verify events arrive in order
  })

  it('Async job lifecycle: submit → poll → result', async () => {
    // Full async flow with polling loop
  })

  it('Worker restart recovery: worker crashes mid-execution', async () => {
    // Kill worker container, restart it, verify stale job is re-claimed
  })
})
```

#### `tests/e2e/load_test.js` (k6 script)

```javascript
// k6 load test — run with: k6 run tests/e2e/load_test.js

// Implement the following scenarios with k6:

// Scenario 1: Baseline (100 VUs × 30s)
// - Each VU submits a simple Python print and waits for result
// - Assert p95 response time < 3000ms
// - Assert error rate < 1%

// Scenario 2: Spike test (ramp from 10 → 500 VUs over 60s)
// - Assert server does not crash
// - Assert rate limiter activates correctly

// Scenario 3: Stress test — malicious inputs
// - Mix of fork bombs, infinite loops, huge allocations
// - Assert system remains stable after each

// Track and export metrics:
// - execution_latency_p50, p95, p99
// - queue_wait_time
// - container_startup_time
// - error_rate_by_type
```

### 3.4 Security Tests (`tests/security/`)

#### `tests/security/test_escape_attempts.py`

A dedicated security test suite that attempts every known container escape technique and verifies each is blocked:

```python
# Each test submits real code that attempts an escape and verifies it fails.
# These must use real Docker execution (not mocks).

ESCAPE_ATTEMPTS = [
    # Filesystem escapes
    ("read_etc_passwd",       "python", "open('/etc/passwd').read()"),
    ("read_host_proc",        "python", "open('/proc/1/cmdline','rb').read()"),
    ("write_etc_hosts",       "python", "open('/etc/hosts','w').write('evil')"),
    ("chroot_escape",         "python", "import os; os.chroot('/')"),
    ("symlink_attack",        "bash",   "ln -s /etc/passwd /tmp/p; cat /tmp/p"),

    # Network escapes
    ("tcp_connect_external",  "python", "import socket; s=socket.socket(); s.connect(('8.8.8.8',53))"),
    ("udp_send",              "python", "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.sendto(b'x',('8.8.8.8',53))"),
    ("http_request",          "python", "import urllib.request; urllib.request.urlopen('http://example.com')"),
    ("dns_lookup",            "python", "import socket; socket.gethostbyname('google.com')"),

    # Privilege escalation
    ("setuid_to_root",        "python", "import os; os.setuid(0)"),
    ("ptrace_self",           "python", "import ctypes; ctypes.CDLL(None).ptrace(0,0,0,0)"),
    ("load_kernel_module",    "bash",   "modprobe xfs"),
    ("write_sysfs",           "bash",   "echo 1 > /sys/kernel/mm/transparent_hugepage/enabled"),

    # Resource attacks
    ("fork_bomb",             "bash",   ":(){ :|:& };:"),
    ("memory_bomb",           "python", "x = ' ' * (10**10)"),
    ("disk_fill",             "bash",   "dd if=/dev/zero of=/tmp/fill bs=1M count=10000"),
    ("cpu_pin_all_cores",     "python", "import os; [os.fork() for _ in range(100)]"),

    # Docker-specific escapes
    ("docker_socket_access",  "bash",   "ls /var/run/docker.sock"),
    ("cgroup_escape",         "bash",   "cat /proc/self/cgroup"),
    ("namespace_read",        "bash",   "ls /proc/1/ns/"),
]

@pytest.mark.parametrize("name,language,code", ESCAPE_ATTEMPTS)
def test_escape_attempt_is_blocked(name, language, code):
    result = execute_code(language=language, code=code)
    assert result.status in ("FAILED", "TIMEOUT", "KILLED"), \
        f"Escape attempt '{name}' was NOT blocked! stdout={result.stdout}"
    assert "/etc/passwd" not in result.stdout, "Passwd content leaked!"
    assert "root:" not in result.stdout, "Root user info leaked!"
```

### 3.5 Test Infrastructure

#### `tests/conftest.py` / `tests/setup.ts`

Provide complete test fixtures:
- `docker_client` — Docker SDK client fixture
- `redis_client` — Redis connection fixture (uses test DB)
- `api_client` — Authenticated HTTP client for API tests
- `ws_client` — WebSocket client factory
- `clean_redis` — Fixture that flushes test keys between tests
- `mock_executor` — Unit test fixture that mocks Docker calls
- `real_executor` — Integration test fixture with real Docker
- `auth_token` — Valid JWT for authenticated requests
- `premium_auth_token` — Token with premium rate limits
- `invalid_auth_token` — Token for auth failure tests

#### Coverage Configuration

Provide `pytest.ini`, `.nycrc` (or `vitest.config.ts`), and CI configuration that:
- Enforces ≥90% line coverage
- Enforces ≥85% branch coverage
- Generates HTML + LCOV reports
- Fails the build if coverage drops

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PART 4 — DOCUMENTATION
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### 4.1 `README.md`
Must include:
- Project overview with feature list and architecture diagram
- Prerequisites (Docker, Node.js, Python, k6)
- 5-minute quickstart: `git clone && make dev && curl example`
- Full environment variable reference table
- Supported languages table with versions
- API quickstart examples (curl + Python SDK + Node.js SDK)
- Security model summary
- Contributing guide
- License

### 4.2 `docs/API.md`
Full API reference with:
- Authentication section (how to get a token, how to pass it)
- For every endpoint: method, path, description, request body (JSON Schema), response body (JSON Schema), all possible HTTP status codes with meanings, curl example, Python example
- WebSocket protocol specification (event types, payload schemas, connection lifecycle)
- Error code catalogue with resolution guidance

### 4.3 `docs/SECURITY.md`
- Isolation model explanation for non-security engineers
- What the sandbox DOES protect against (enumerated)
- What the sandbox does NOT protect against (known limitations, e.g. Spectre)
- How to report a security vulnerability (responsible disclosure policy)
- CVE response process
- Hardening checklist for production deployment

### 4.4 `docs/ADDING_LANGUAGE.md`
Step-by-step guide to add a new language runtime, with a worked example (adding `lua`):
1. Create `runtimes/lua/Dockerfile`
2. Create `runtimes/lua/seccomp-profile.json`
3. Register in `worker/sandbox/image_registry.py`
4. Add resource defaults in `worker/sandbox/resource_limits.py`
5. Build and scan image
6. Add language tests to test suite
7. Update `docs/API.md` languages table

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PART 5 — CROSS-CUTTING REQUIREMENTS
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every piece of code you produce must satisfy ALL of the following. These are not optional:

### Observability
- Every execution emits structured JSON logs with: `job_id`, `language`, `user_id`, `duration_ms`, `exit_code`, `oom_killed`, `timed_out`, `worker_id`
- Prometheus metrics exported for: `sandbox_executions_total{language, status}`, `sandbox_execution_duration_seconds{language}`, `sandbox_queue_depth`, `sandbox_container_startup_seconds`, `sandbox_oom_kills_total`, `sandbox_timeout_kills_total`
- Distributed tracing via OpenTelemetry with trace context propagated from HTTP request through queue to worker execution

### Error Handling
- All errors are typed and carry structured context (never raw exception messages to clients)
- All async operations have explicit error handling (no unhandled promise rejections)
- Network/Redis/Docker transient failures trigger exponential backoff with jitter (max 3 retries)
- Circuit breaker pattern around Docker daemon calls (if Docker is down, fail fast)

### Code Quality
- TypeScript: strict mode enabled (`"strict": true`), zero `any` types, ESLint with `@typescript-eslint` ruleset
- Python: type annotations on all functions, mypy with `strict` mode, ruff for linting and formatting
- All functions have docstrings/JSDoc with parameter descriptions
- No magic numbers — all constants are named and documented
- Maximum function length: 50 lines (split larger functions)

### Security Hygiene
- No secrets in code or Docker images (all via environment variables)
- All user-supplied strings are treated as untrusted data (never shell-interpolated, never used in file paths without sanitization)
- JWT secrets are validated at startup (fail if weak or missing)
- CORS configured with explicit origin allowlist (not `*`)
- `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options` headers set

### CI/CD (`/.github/workflows/`)
Provide complete GitHub Actions workflows for:
- `ci.yml` — on every PR: lint, type-check, unit tests, build Docker images, integration tests
- `security.yml` — nightly: Trivy scan all images, Snyk dependency audit, OWASP ZAP scan on running API
- `release.yml` — on tag push: build + push images, run e2e tests, create GitHub release with changelog

---

## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PART 6 — DELIVERY FORMAT
## ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Deliver the output in the following format:

1. **Start with `docs/ARCHITECTURE.md`** — the complete architecture document from Part 1. This must be the foundation everything else builds on.

2. **Then deliver each source file** in this exact order, preceded by a code block header comment showing the file path:
   ```
   // FILE: api/src/config.ts
   ```
   Every file must be complete and runnable.

3. **Deliver test files last**, in the same format.

4. After all files, provide a **"Getting Started in 5 Minutes"** section with the exact commands to clone, configure, start, and make a first API call.

5. End with a **"Known Limitations and Future Work"** section that honestly lists:
   - Security limitations of the current design
   - Performance bottlenecks at scale
   - Features not yet implemented (with implementation difficulty estimates)

---

## ════════════════════════════════════════════════
## END OF MASTER PROMPT
## ════════════════════════════════════════════════

---

## USAGE NOTES FOR THIS PROMPT

### How to Use
Paste everything between the dashed lines (the "MASTER PROMPT" section) directly into your AI system as a single user message. No system prompt modifications are needed.

### Recommended Models
- **Claude Opus 4 / Claude Sonnet 4+** — For full end-to-end generation including all code files. Use extended thinking mode if available.
- **GPT-4o / o1** — Comparable capability; may need to be chunked by section.

### Prompting Strategy for Best Results
If the model's output window is too small for everything at once, break it into phases:

```
Phase 1: "Execute Parts 1 only (Architecture Document)"
Phase 2: "Execute Part 2, Sections 2.1–2.2 (Project structure + API server)"
Phase 3: "Execute Part 2, Sections 2.3–2.4 (Worker + Runtimes + Compose)"
Phase 4: "Execute Part 3 (Full test suite)"
Phase 5: "Execute Part 4 (Documentation)"
```

Add this prefix to each phase request:
> "Continuing the Code Interpreter Sandbox project. Previous context: [paste ARCHITECTURE.md summary]. Now implement [phase]."

### Quality Checklist After Generation
Verify the output satisfies:
- [ ] Architecture document covers all 10 sections
- [ ] `executor.py` uses ALL 14+ Docker security flags
- [ ] Seccomp profiles are valid JSON with `defaultAction: SCMP_ACT_ERRNO`
- [ ] Every unit test class has zero TODO/stub methods
- [ ] All 25+ escape attempt security tests are included
- [ ] k6 load test script is complete
- [ ] GitHub Actions CI workflow is complete
- [ ] README has a working 5-minute quickstart

### Extension Ideas
After the base system is working, use follow-up prompts for:
- **Jupyter-style sessions**: "Add stateful REPL sessions with persistent kernel per user"
- **File uploads**: "Add support for users uploading data files (CSV, JSON) that code can read"
- **AI-powered analysis**: "Add a /v1/analyze endpoint that uses Claude to explain execution errors"
- **Multi-file projects**: "Support zip uploads containing multiple source files"
- **Custom packages**: "Add a pre-approved package allowlist with cached pip/npm installs"
