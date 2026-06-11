# Security Hardening Guide

This document describes the security model of the **Code Interpreter Sandbox** — a
self-hosted service that executes arbitrary, untrusted user code inside ephemeral,
single-use Docker containers. It is written for two audiences: engineers running the
service in production, and reviewers who need to understand exactly what the sandbox
does and does not guarantee.

The guiding assumption throughout is simple and absolute:

> **Everything that runs inside a sandbox container is hostile.**
> User code may be actively malicious, may be trying to escape, and may be probing for
> any weakness in the host. The containment is the product.

---

## 1. Isolation model for non-security engineers

### The trust boundary

Requests flow through a chain of components, and trust *decreases* at every hop until it
hits zero inside the container:

```
  Internet  ──►  API (Express)  ──►  Queue (Redis)  ──►  Worker  ──►  Container
 (untrusted)   (auth, rate limit,   (durable handoff)  (orchestrates  (UNTRUSTED CODE —
                input validation)                       Docker)        assume hostile)
```

- The **API** authenticates the caller, rate-limits, and validates input. It never runs
  user code.
- The **Queue** is a durable handoff so a worker crash never loses a job. Redis is an
  internal service and must never be exposed to the internet.
- The **Worker** orchestrates Docker. It builds the locked-down `docker run` command,
  streams output, and enforces the wall-clock timeout. It talks to the Docker daemon but
  **never** mounts the Docker socket into a sandbox.
- The **Container** is where the untrusted code actually runs. Everything inside it is
  treated as an attacker who already has code execution.

### The "8 locked doors"

Containment is **defense in depth**: not one wall, but eight independent layers. An
attacker has to defeat *all* of them, and most layers were designed so that defeating one
still leaves the others standing.

| # | Door | What it does in plain English |
|---|------|-------------------------------|
| 1 | **Linux namespaces** (PID/NET/MNT/UTS/IPC/USER) | The container gets its own private view of processes, network, filesystem mounts, hostname, IPC, and user IDs. It cannot see or signal anything on the host. |
| 2 | **Filesystem** | The root filesystem is read-only. The only writable space is a 64 MB in-memory `/tmp` that is mounted `noexec` (you cannot run a program from it). The only thing mounted from outside is the per-job code directory, mounted **read-only** at `/sandbox`. No host directories are bind-mounted. |
| 3 | **Network** | By default the container has *no network at all* (`--network none`). It cannot reach the internet, the host, the cloud metadata service, or any internal service. |
| 4 | **Seccomp** | A kernel-level allowlist of system calls. Anything not explicitly permitted is blocked with an error. The default is *deny everything*. |
| 5 | **Capability dropping** | Every Linux "superpower" (capability) is removed. The process cannot do anything that requires elevated privilege, even in theory. |
| 6 | **Cgroups (resource limits)** | Hard caps on memory, CPU, process count, open files, and disk so a single job cannot starve the host. |
| 7 | **User namespace remapping** | The container runs as `nobody` (uid 65534), and with user-namespace remapping that maps to an unprivileged user on the host. "Root in the container" is not root on the host. |
| 8 | **Immutable infrastructure** | Runtime images are pinned by SHA-256 digest and verified before every run. No software is installed at runtime, so there is no opportunity to pull in a malicious package. |

If you remember one thing: **the container starts with nothing and is granted only the
bare minimum to run one program once.**

---

## 2. What the sandbox DOES protect against

Each protection below maps to concrete flags and layers in the implementation
(`worker/sandbox/executor.py`, `worker/sandbox/seccomp.py`,
`worker/sandbox/resource_limits.py`).

### Arbitrary host command execution

The worker drives Docker through **argv arrays only — never a shell**
(`asyncio.create_subprocess_exec`, not a shell string). User input is never interpolated
into a command line, so there is no shell-injection surface in the orchestration path.
Inside the container, the code runs as `nobody` with a read-only root, dropped
capabilities, and `no-new-privileges`, so even full code execution inside the sandbox
cannot reach the host.

### Host filesystem escape / secret theft

- `--read-only` root filesystem: nothing on the container's root can be written.
- The only writable area is `--tmpfs=/tmp:rw,noexec,nosuid,nodev` (in-memory, size-bounded,
  non-executable). It vanishes when the container exits.
- **No host bind mounts.** The only mount from outside is the per-job code directory at
  `/sandbox`, mounted **read-only** (`:ro`).
- The seccomp `DANGEROUS_SYSCALLS` set blocks `mount`, `umount2`, `pivot_root`, `chroot`,
  the new mount API (`fsopen`/`fsconfig`/`move_mount`/`open_tree`), and the handle-based
  escape calls (`open_by_handle_at`, `name_to_handle_at`). There is no path to remount or
  reach the host filesystem.
- Filenames supplied by the user are sanitized to a safe basename (`_safe_basename`) and
  any path that would escape the per-job dir is rejected (`path_escape`), so a job cannot
  write outside its own directory even on the worker.

### Fork bombs, infinite loops, memory bombs, disk fill

| Threat | Control |
|--------|---------|
| Fork bomb | `--pids-limit=<N>` (cgroup `pids.max`) + `--ulimit nproc=<N>`. Bash gets the strictest cap (32 PIDs) because shell fork bombs are a one-liner. |
| Infinite loop | Wall-clock timeout enforced by the worker (see §below), plus `--cpus=<f>` CPU bandwidth caps. |
| Memory bomb | `--memory=<N>m` with `--memory-swap=<N>m` set equal (swap fully disabled — no swap escape hatch). OOM is detected via container exit code **137**. |
| Disk fill | `--tmpfs ...size=<N>m` bounds the writable scratch; `--ulimit fsize=<bytes>` caps any single file. |
| FD exhaustion | `--ulimit nofile=256:256`. |

### Timeout and output enforcement

- Wall-clock timeout is enforced by the worker: **SIGTERM → 2-second grace → SIGKILL**
  (`--stop-timeout 2` reinforces this at the Docker level).
- Per-language timeouts are clamped to a hard ceiling (`MAX_TIMEOUT_SECONDS = 60`).
- Combined stdout+stderr is capped (default **1 MiB**); output beyond the cap is truncated
  and flagged, preventing output flooding from exhausting worker memory.

### Network exfiltration & SSRF (incl. cloud metadata)

- `--network=none` by default: no sockets can reach anything outside the container.
- The network syscalls (`socket`, `connect`, `sendto`, …) are **not** in the seccomp
  allowlist unless network egress is explicitly enabled, so even attempting to open a
  socket fails. (`socketpair` is allowed because it only creates a local AF_UNIX pair with
  no external reachability.)
- This closes SSRF entirely in the default configuration: the cloud metadata endpoint at
  `169.254.169.254`, link-local addresses, internal services, and the host are all
  unreachable.
- When egress *is* required, it should go through an **egress proxy with a DNS sinkhole**
  (allowlisted destinations only) rather than raw networking.

### Privilege escalation

Four independent controls, any one of which would block escalation on its own:

- `--cap-drop ALL` and add nothing back — no `CAP_SYS_ADMIN`, `CAP_NET_RAW`, etc.
- `--security-opt no-new-privileges` — setuid/setgid binaries cannot gain privilege.
- `--user nobody` (uid 65534) — never runs as root in the container.
- seccomp blocks the entire setuid/setgid family (`setuid`, `setgid`, `setresuid`,
  `setgroups`, `capset`, …) plus `ptrace`, `process_vm_readv/writev`, so a process cannot
  change identity or peek into another process's memory.

### Kernel-module loading

`init_module`, `finit_module`, `delete_module`, `create_module`, and friends are in
`DANGEROUS_SYSCALLS` and blocked in every profile. `bpf`, `kexec_load`, and
`perf_event_open` are also denied, removing common kernel-attack primitives.

### `/proc` & `/sys` tampering

Combined with the read-only root, dropped capabilities, and the seccomp denials of
`_sysctl`, `sysfs`, `settimeofday`/`clock_settime`/`adjtimex`, `acct`, `quotactl`,
`swapon`/`swapoff`, and `reboot`, user code cannot reconfigure the kernel or tamper with
host-visible pseudo-filesystems.

### Docker-socket breakout

**The Docker socket is never mounted into a sandbox container.** A container with the
Docker socket is equivalent to root on the host; we never grant it. The worker reaches
Docker via the host daemon socket *outside* the sandbox boundary. In production this is
further hardened (see §6): rootless Docker, a socket-proxy with a least-privilege API
allowlist, or the gVisor (`runsc`) runtime for a second kernel boundary.

### Dependency-confusion / supply-chain

- **No runtime package installs.** Images are built ahead of time and frozen; there is no
  `pip install` / `npm install` at execution time, so there is no dependency-confusion
  window.
- Images are **pinned and verified by SHA-256 digest** before every run
  (`_verify_image` in `executor.py` raises `ImageIntegrityError` on mismatch). In
  production images should also be cosign-signed and verified.
- Runtime entrypoints are locked down (e.g. Python runs with `-I -B` for isolated mode;
  Ruby with `--disable-gems`).

### Output flooding / log injection

The combined 1 MiB output cap bounds memory and log volume. Output is treated as opaque
bytes, decoded defensively (`errors="replace"`), and returned as structured data — never
evaluated.

### Auth bypass

- JWT verification is an in-house HS256 implementation (`api/src/services/jwt.ts`) that
  **pins the algorithm to HS256** to prevent algorithm-confusion (`alg: none`, RS256→HS256)
  attacks, and uses `timingSafeEqual` for signature comparison.
- The `JWT_SECRET` is validated at startup (`api/src/config.ts`): it must be **≥ 32
  characters** and must not be a known-weak placeholder (`secret`, `changeme`, `password`,
  …). The process refuses to boot otherwise.
- API keys are matched against a configured map and surfaced only as a hashed fingerprint
  in the principal id.
- Anonymous access is permitted **only** when `ALLOW_ANONYMOUS=true` is explicitly set.

### Code injection via filenames / argv

The in-container command is constructed as an **argv array** with the source path appended
positionally (`RuntimeConfig.argv_for`); it is never passed through a shell. Source and
input filenames are fixed or sanitized (`_safe_basename`: rejects NUL bytes, `.`/`..`,
non-`[A-Za-z0-9._-]` characters, and names over 255 chars). A set of dangerous environment
variables (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `BASH_ENV`, …)
can never be overridden by the user (`_FORBIDDEN_ENV`).

### Seccomp design detail

- Default action: `SCMP_ACT_ERRNO` (**default-deny** — unknown syscalls fail with an
  errno rather than executing).
- Per-language allowlists: `COMMON_SYSCALLS` (~219 calls) for interpreted/compiled
  languages, plus small per-language extras; `BASH_SYSCALLS` (~81 calls) makes **bash the
  most restrictive** profile.
- `DANGEROUS_SYSCALLS` is **subtracted from every profile** defensively — those calls can
  never be allow-listed, even by mistake in a future edit.
- `clone` is allowed only with a **masked-argument rule** that forbids the
  namespace-creation flags (mask `0x7E020000`); `clone3` returns **ENOSYS** to force the
  glibc fallback onto the restricted `clone`.
- The same builder (`worker/sandbox/seccomp.py`) generates the static
  `runtimes/<lang>/seccomp-profile.json` artifacts via `make seccomp-regen`.

---

## 3. What the sandbox does NOT protect against (known limitations)

We are deliberately honest about the boundaries of the model. The following are **not**
fully solved by this architecture; each has a mitigation and a recommended path to higher
assurance.

### Transient-execution / microarchitectural side channels

Spectre, Meltdown, MDS, and cache-timing attacks operate *below* the syscall and namespace
layer, exploiting the shared CPU and its caches. On a shared kernel, a malicious tenant
*may* be able to observe timing signals from co-tenants.

- **Mitigated, not eliminated.** Run hosts with KPTI and retpoline (and current
  microcode), keep the host kernel patched, and disable SMT/hyper-threading for the most
  sensitive deployments.
- **For high assurance:** use the gVisor (`runsc`) runtime for a user-space kernel
  boundary, or isolate tenants onto **per-tenant nodes or microVMs** (e.g. Firecracker) so
  there are no co-tenants to attack.

### Kernel 0-day local privilege escalation

The sandbox shares the host kernel. A previously-unknown kernel vulnerability reachable
from an allowed syscall could, in principle, be exploited.

- **Mitigated by** the seccomp default-deny posture (a tiny syscall surface), dropped
  capabilities, `no-new-privileges`, and running as `nobody`. The smaller the syscall
  surface, the fewer 0-days are reachable.
- **Not impossible.** For a second, independent kernel boundary use **gVisor** or
  **Firecracker microVMs**, and keep the host kernel patched aggressively.

### Resource-based denial of service at extreme scale

Per-job limits (memory, CPU, PIDs, disk, timeout, output) bound a *single* job, but a
flood of jobs can still pressure the cluster.

- **Mitigated by** tiered rate limiting, per-user concurrency quotas, and queue
  backpressure. For sustained abuse, scale horizontally and apply upstream WAF/network
  controls.

### Rate-limiter fails open during a Redis outage

The sliding-window rate limiter and the concurrency quota **fail open** if Redis is
unreachable — they log a warning and allow the request. This is a deliberate
**availability-over-strictness** trade-off (`api/src/middleware/rateLimiter.ts`,
`api/src/services/quota.ts`): a Redis blip should not take down the whole API. The
consequence is that, during a Redis outage, rate limits and concurrency caps are not
enforced. Mitigate by running Redis HA with monitoring and alerting on the
"failing open" log line, and front the API with an independent upstream limiter (WAF / LB)
for a backstop.

---

## 4. Responsible disclosure policy

We welcome reports from security researchers and treat them as a priority.

### How to report

- Email **security@your-org** (replace `your-org` with your deployment's domain) with a
  clear description, reproduction steps, affected version/commit, and impact assessment.
- PGP is optional; if you require encryption, request our public key in your first
  (non-sensitive) message.
- Please do **not** open a public GitHub issue for a suspected vulnerability.

### What to expect

| Stage | Target |
|-------|--------|
| Acknowledgement of your report | within **48 hours** |
| Initial triage and severity assessment | within **5 business days** |
| Coordinated public disclosure | within **90 days** of the report, or sooner if a fix ships earlier |

We will keep you informed of progress and credit you in the advisory unless you prefer to
remain anonymous.

### Safe harbor

We will not pursue legal action against researchers who:

- act in good faith and avoid privacy violations, data destruction, and service
  degradation;
- give us reasonable time to remediate before public disclosure;
- only interact with accounts they own or have explicit permission to test.

Activity conducted consistent with this policy is considered authorized.

### Please do not test against shared or production infrastructure

Run your testing against a **local or dedicated instance** that you control. Do not run
exploit attempts, fuzzing, denial-of-service, or load tests against our shared or
production environments — doing so endangers other users and is outside the safe-harbor
scope.

---

## 5. CVE response process

### Continuous scanning

- **Trivy** scans every built image in CI via `make security-scan`, which fails the build
  on `HIGH` or `CRITICAL` findings (`trivy image --severity HIGH,CRITICAL --exit-code 1`).
- A **nightly `security.yml`** workflow re-scans the published images so a CVE disclosed
  *after* a build is still caught.
- **Dependabot / Snyk** track dependency vulnerabilities for the API (npm) and worker
  (Python) dependency trees and open update PRs.

### Triage → patch → rebuild → redeploy → rotate-digests

1. **Triage** the finding: confirm it affects a shipped image/dependency and is reachable
   in our usage; assign a severity.
2. **Patch**: bump the base image or dependency, or apply the fix.
3. **Rebuild** images (`make build`) and re-run `make security-scan` to confirm the finding
   is resolved.
4. **Redeploy** the rebuilt images.
5. **Rotate digests**: update the pinned `SANDBOX_DIGEST_<LANG>` values (and cosign
   signatures) so the worker's digest verification accepts only the patched images and
   rejects the vulnerable ones.

### Remediation SLAs by severity

| Severity | Remediation target |
|----------|--------------------|
| Critical | **24–48 hours** |
| High | **7 days** |
| Medium | **30 days** |
| Low | Next scheduled release |

---

## 6. Production hardening checklist

Work through this before exposing the sandbox to untrusted users. Items map to the
configuration in `api/src/config.ts`, `worker/config.py`, and the Docker run flags in
`worker/sandbox/executor.py`.

### Authentication & API surface

- [ ] **Strong, random `JWT_SECRET`** (≥ 32 chars, high entropy — e.g.
      `openssl rand -base64 48`). The app refuses to boot on a short or known-weak value.
- [ ] **`ALLOW_ANONYMOUS=false`** in production — require a JWT or API key for every
      request.
- [ ] **Explicit `CORS_ORIGINS` allowlist** — never `*` (the config rejects `*` in
      production). List only the exact front-end origins.
- [ ] **Rotate API keys** regularly and on suspected compromise; store them via a secret
      manager, never in source or images.
- [ ] Confirm security headers are active (helmet): CSP `default-src 'none'`,
      `X-Frame-Options: deny`, `X-Content-Type-Options: nosniff`,
      `frame-ancestors 'none'`.

### Transport & network

- [ ] **TLS terminates at the load balancer**; the API trusts exactly one proxy hop
      (`trust proxy: 1`).
- [ ] **Network-segment Redis and MinIO** onto a private network — never internet-exposed.
- [ ] Keep the default **`--network none`** for sandbox containers; if egress is required,
      route it through an **egress proxy with a DNS sinkhole** and a destination allowlist.

### Worker / container runtime

- [ ] Run workers on a **dedicated, hardened node pool** separate from API/control-plane
      nodes.
- [ ] Use a **second kernel boundary**: gVisor (`runsc`) runtime or **Firecracker**
      microVMs for the strongest tenant isolation.
- [ ] Use **rootless Docker** or a **socket-proxy with a least-privilege API allowlist** —
      never expose the full Docker socket. The socket is never mounted into sandboxes.
- [ ] **Enable user namespaces** on the Docker daemon (`userns-remap`) so container
      `nobody` maps to an unprivileged host uid.
- [ ] Apply **AppArmor or SELinux** profiles and the host seccomp default on the worker
      host itself, as a layer beneath the per-job profiles.

### Host

- [ ] **Keep the host kernel patched** and enable **KPTI / retpoline** (and current
      microcode) against transient-execution attacks. Consider disabling SMT for sensitive
      tenants.
- [ ] **Pin runtime images by digest** (`SANDBOX_DIGEST_<LANG>`) and verify with
      **cosign** signatures before deploy. The worker rejects a digest mismatch at run time.
- [ ] Set **resource limits on every service** (API, worker, Redis, MinIO) at the
      orchestrator level, not just on sandbox jobs.

### Data & availability

- [ ] **Enable Redis AOF** (append-only file) for durability of the queue and rate-limit
      state; run Redis HA with monitoring.
- [ ] **Least-privilege secrets** via a secret manager (Vault, cloud KMS/secret store) —
      never bake secrets into images or commit them.
- [ ] Run regular **disaster-recovery drills** (restore from backup, fail over Redis/MinIO,
      rebuild the worker pool).

### Observability

- [ ] **Centralized audit logging** (e.g. Loki) with structured request ids, and **alerts**
      on security-relevant events — including the rate-limiter / quota
      "failing open" warnings, image-integrity failures, and auth failures.
- [ ] Monitor container exit code **137** (OOM) and timeout rates as abuse signals.

---

*Security is layered, not absolute. Keep this document in sync with the implementation —
when a flag, syscall list, or limit changes, update the corresponding section here and
regenerate the static seccomp profiles (`make seccomp-regen`).*
