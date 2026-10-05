# Sandbox internals

How one submission becomes one container, what each isolation layer really does, and what it
does not do. Everything here is derived from `worker/sandbox/*.py` and the runtime Dockerfiles;
file references are given so you can check them. For the attacker-centred view see
[THREAT_MODEL.md](THREAT_MODEL.md); for measured behaviour see [BENCHMARKS.md](BENCHMARKS.md).

## 1. Lifecycle of one execution

`SandboxExecutor.execute()` (`worker/sandbox/executor.py`) does, in order:

1. `get_runtime(language)` - resolve image, entrypoint, source filename and limits
   (`image_registry.py`). Unknown language raises `UnknownLanguageError`.
2. `_verify_image` - `docker image inspect <image> --format {{json .RepoDigests}}`. Failure with
   "cannot connect to the docker daemon" raises `DockerUnavailableError` (retryable, the message is
   left un-acked); any other failure raises `ImageNotFoundError`; a configured
   `SANDBOX_DIGEST_<LANG>` that is absent from `RepoDigests` raises `ImageIntegrityError`.
3. `_prepare_workdir` - `mkdtemp(<job_id>-…)` under `SANDBOX_WORKDIR` (mode 0755), write the
   source file (fixed name per language, e.g. `main.py`, `Main.java`) and each input file at mode
   0644 after `_safe_basename` (alphanumerics, `.`, `_`, `-`; directories stripped) and a
   `realpath` containment check.
4. `_build_command` - build the `docker run` argv (section 2). argv list, never a shell.
5. `_run` - spawn, feed stdin, pump stdout/stderr into a capped buffer (and to the live-stream
   callback), sample cgroup stats every 50 ms, and wait for exit, timeout or cancellation.
6. `_build_result` - map the outcome to `ExecutionResult`.
7. `finally: _cleanup` - `docker rm -f sandbox-<job_id>` and `shutil.rmtree(workdir)`. This runs
   on every path, including exceptions.

Outcome mapping (`_build_result`):

| Condition | `status` | `exit_code` | Flags |
|---|---|---|---|
| process exited, code != 137 | `COMPLETED` | the code (non-zero is still `COMPLETED`) | |
| process exited with 137 and we did not kill it | `FAILED` | 137 | `oom_killed = true` |
| wall-clock timeout fired | `TIMEOUT` | `null` | `timed_out = true` |
| cancel event fired | `KILLED` | `null` | |

Two consequences worth knowing. OOM is inferred from exit code 137, so a program that
deliberately exits 137 or kills itself with `SIGKILL` is reported as an OOM kill (`--rm` removes
the container before `docker inspect` could read `.State.OOMKilled`). And the timeout clock starts
when `docker run` is spawned, so container creation time is *inside* the budget.

### Timeout and kill ladder

`_await_completion` waits on `proc.wait()`, the optional cancel event and `timeout`. On timeout
or cancel, `_terminate`:

1. `docker kill --signal=TERM <name>`;
2. wait up to `SIGTERM_GRACE_SECONDS` (default 2) for `docker run` to return;
3. `docker kill --signal=KILL <name>`;
4. wait up to 5 s more; if still alive (for instance the kill raced container creation and found
   nothing) `docker rm -f` and kill the local `docker run` client. This last step was added in this
   branch; before it the worker awaited the process without bound.

Why `--init` matters (added in this branch): without it the interpreter is the container's PID 1,
and the kernel does not deliver a signal that has no handler to a PID namespace's init when it is
sent from outside the namespace. Nearly every program (a Python spin loop, `sleep`) therefore
*ignored* the TERM of step 1 and was only stopped by the KILL of step 3, so every timeout cost an
extra `SIGTERM_GRACE_SECONDS`. With `--init`, docker's `tini` is PID 1, forwards the signal, and a
program with default signal handling dies on TERM (exit 143). Programs that install their own
handler and ignore it are still KILLed after the grace period. Before/after numbers are in
[BENCHMARKS.md](BENCHMARKS.md#timeout-enforcement).

## 2. The `docker run` command

Generated for Python with the default config (verbatim from `_build_command`, one flag per line
here for readability):

```text
docker run --rm -i --name sandbox-<job_id>
  --read-only --cap-drop ALL
  --security-opt no-new-privileges
  --security-opt seccomp=/tmp/sandbox-seccomp/python.json
  --user nobody --hostname sandbox
  --label sandbox-managed=1 --stop-timeout 2 --init
  --memory=256m --memory-swap=256m --cpus=0.5 --pids-limit=64
  --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m
  --ulimit nofile=256:256 --ulimit nproc=64:64 --ulimit fsize=67108864
  --network=none
  --workdir /tmp -e SANDBOX=1 -e HOME=/tmp -e TMPDIR=/tmp [-e user vars]
  -v <host workdir>/<job>:/sandbox:ro
  sandbox-runtime-python:latest python3 -I -B /sandbox/main.py
```

Go and Rust additionally get `--tmpfs /build:rw,exec,nosuid,nodev,size=<disk>m,mode=1777` and use
`/build` as workdir and `HOME`, because compiled output must be executable while `/tmp` stays
`noexec` (`needs_exec_build` in the registry).

User-supplied environment variables are appended as `-e KEY=VALUE` unless the key (case
-insensitively) is in `_FORBIDDEN_ENV` (`PATH`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`,
`HOME`, `TMPDIR`, `NODE_OPTIONS`, `PYTHONPATH`, `PYTHONSTARTUP`, `BASH_ENV`, `ENV`, `IFS`,
`RUBYOPT`, `GEM_PATH`, `CLASSPATH`, `JAVA_TOOL_OPTIONS`, `SANDBOX`), contains `=`, or is empty.
The API applies a similar but not identical list first (it also strips `SHELLOPS` and `PERL5LIB`,
and requires `^[A-Za-z_][A-Za-z0-9_]*$`). Neither list is a security boundary: the user already
controls the program that runs inside the container.

## 3. Isolation layers, one by one

### 3.1 Namespaces

Docker's defaults apply: new PID, mount, network, UTS and IPC namespaces. The code never sees
host processes (`/proc` shows only the container). **No user namespace is created**: unless the
Docker daemon is configured with `userns-remap`, container uid 65534 maps to host uid 65534. The
older docs listed "user namespace remapping" as an always-on layer; it is an optional daemon-level
hardening (see [DEPLOYMENT.md](DEPLOYMENT.md#host-hardening)). Seccomp additionally denies
`unshare`, `setns` and any `clone()` carrying a `CLONE_NEW*` flag, so the sandboxed process cannot
create namespaces of its own.

### 3.2 Filesystem

* `--read-only` root file system.
* `/tmp` is a tmpfs: `rw,noexec,nosuid,nodev`, size = the language's `disk_mb`. tmpfs pages are
  charged to the container's memory cgroup, so `/tmp` usage competes with the `--memory` limit
  rather than adding to it.
* The per-job directory is bind-mounted **read-only** at `/sandbox`. It is the only host path
  exposed. Source and input files are 0644 in a 0755 directory so `nobody` can read them.
* `--ulimit fsize=<disk_mb * 1048576>` is in **bytes** (RLIMIT_FSIZE); one file cannot exceed the
  tmpfs size anyway.
* Compiled languages get a second tmpfs, `/build`, with `exec`.

### 3.3 Network

`--network=none` (every runtime has `network_enabled=False`; there is no API to turn it on).
The container has only a loopback interface. For the five allow-list languages `socket()` is also
absent from the seccomp allow-list; `socketpair` is allowed for local IPC. For the three
block-list languages (TypeScript, Go, Rust) `socket()` is allowed by seccomp but has nowhere to
connect.

### 3.4 Seccomp (`worker/sandbox/seccomp.py`)

Profiles are generated from code by `build_profile()` and written to
`$SECCOMP_CACHE_DIR/<lang>.json` on first use. The committed
`runtimes/<lang>/seccomp-profile.json` files are byte-identical outputs of
`python -m worker.sandbox.seccomp <lang>` (checked in this audit) and are not read at run time;
`make seccomp-regen` refreshes them.

| Languages | Model | Details |
|---|---|---|
| python, javascript, java, ruby, bash | **default-deny** allow-list | `defaultAction: SCMP_ACT_ERRNO`; allow `COMMON_SYSCALLS` (219 names for Python) plus a few per-language extras; `clone` allowed only with `(flags & 0x7E020000) == 0`; `clone3` returns `ENOSYS` so glibc falls back to `clone`. |
| typescript, go, rust | **default-allow** block-list | The toolchains (esbuild/tsx, the Go linker, rustc+cc) need too many syscalls to enumerate. `DANGEROUS_SYSCALLS` return `EPERM`; `clone3` returns `ENOSYS`; one deny rule per `CLONE_NEW*` flag. |

`DANGEROUS_SYSCALLS` is subtracted from every allow-list defensively. It covers `ptrace`,
`process_vm_*`, `kcmp`, module loading, `mount`/`umount*`/`pivot_root`/`chroot`, the new mount API,
`unshare`/`setns`, `bpf`, keyrings, `perf_event_open`, `userfaultfd`, `io_uring_*`, AIO, `kexec_*`,
`reboot`, `swapon/off`, clock setting, `acct`, `quotactl`, `ioperm/iopl/modify_ldt/vm86`,
`personality`, `open_by_handle_at`/`name_to_handle_at`, `fanotify_*`.

Trade-off to be aware of: a block-list profile fails open for syscalls added to the kernel after
the list was written. The remaining layers (cap-drop, no-new-privileges, no network, read-only
root, non-root user) are what make that acceptable; see
[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md#adr-004-block-list-seccomp-for-toolchain-runtimes).

### 3.5 Capabilities and privilege

`--cap-drop ALL` and `--security-opt no-new-privileges`; `--user nobody`. The runtime images also
delete `su`, `sudo`, `passwd`, `mount`, `umount`, `chmod`, `chown` and common network clients
(`wget`, `curl`, `nc`, `ssh`, ...) from the file system (see each `runtimes/*/Dockerfile`; the list
differs slightly per image). Removing binaries is hygiene, not a boundary: a program can ship its
own.

### 3.6 cgroups v2 limits

| Flag | Mechanism | Notes |
|---|---|---|
| `--memory=N --memory-swap=N` | `memory.max`, swap disabled | OOM kill is delivered to the container's processes. Includes tmpfs. |
| `--cpus=f` | `cpu.max` quota/period | Bandwidth limit, not pinning; a 0.5 limit still lets a spin loop use 50% of a core. |
| `--pids-limit=N` | `pids.max` | Per-container fork-bomb cap. |
| `--ulimit nofile / nproc / fsize` | RLIMIT_* | `RLIMIT_NPROC` counts processes **per real uid across the host**, not per container. All sandboxes run as uid 65534, so concurrent sandboxes share one `nproc` budget; the cgroup `pids.max` is the effective per-container control. |

### 3.7 Output limits

`_OutputCapture` keeps stdout+stderr up to `output_max_bytes` (1 MiB combined), then sets
`truncated`. The reader keeps *draining* the pipes after the cap (so the container is not blocked
on a full pipe) but discards the data; the loop is bounded by the timeout. Live-stream callbacks
receive only the kept portion.

### 3.8 Resource sampling

Every 50 ms `_sample` reads `memory.peak` (falling back to `memory.current`, then cgroup v1 paths)
and `cpu.stat usage_usec` from the host cgroup tree (`/sys/fs/cgroup/system.slice/docker-<id>.scope`
or `/sys/fs/cgroup/docker/<id>`), resolving the container id once with `docker inspect`. The worker
container therefore needs `/sys/fs/cgroup` mounted read-only (compose does this). If no path
matches, `memory_bytes` and `cpu_time_ms` are `0` and nothing else is affected.

## 4. Cleanup and garbage collection

| Mechanism | Where | What it covers |
|---|---|---|
| `--rm` | `docker run` flag | Normal exit. |
| `docker rm -f` in `finally` | `_cleanup` | Any exit path including timeout/cancel/exception. |
| Work-dir `rmtree` in `finally` | `_cleanup` | Per-job code directory. |
| `ContainerReaper` | `worker/sandbox/cleanup.py`, every 60 s | Containers with label `sandbox-managed=1`: removes `Exited`/`Dead`/`Removal*`; removes `Up` containers older than 300 s; removes `Created` containers older than 300 s (a fresh `Created` is the normal state of a container about to start and is left alone); deletes entries in `SANDBOX_WORKDIR` with mtime older than 300 s. |
| Stale-entry reclaim | `QueueConsumer.claim_stale` | Jobs of a dead worker are re-run by another worker after 60 s idle. |

If the worker process is `kill -9`'d mid-job, the sandbox container keeps running until its own
`--memory`/`--pids` limits or the reaper's 300 s threshold removes it; the *timeout* is enforced by
the worker, not by Docker, so a dead worker means no timeout. This is a deliberate simplicity
trade-off (see ADR-002).

## 5. What runs where

```text
host
 +- dockerd  <---- /var/run/docker.sock ----+
 +- worker container (root, python)  -------+   <- trusted; can create any container
 |    +- docker CLI  ->  docker run ... sandbox-runtime-X   (sibling containers)
 |    +- /var/sandbox-work  == host path (bind), per-job dirs
 +- sandbox container (nobody, no caps, no net, ro rootfs)   <- hostile
```

The worker is the most privileged component: access to the Docker socket is root-equivalent on
the host. It never runs user code itself. Protect it as you would the host (see
[THREAT_MODEL.md](THREAT_MODEL.md)).

## 6. Per-runtime notes (`runtimes/*/Dockerfile`)

| Runtime | Base image | Entry | Notes |
|---|---|---|---|
| python | `python:3.12.8-slim-bookworm` | `python3 -I -B main.py` | `-I` isolated mode ignores `PYTHON*` env and user site; `-B` no bytecode. |
| javascript | `node:20.18.1-alpine3.20` | `node main.js` | BusyBox applets `wget`/`nc` removed. |
| typescript | `node:20.18.1-alpine3.20` + tsx 4.19.2, tsc 5.7.2 | `/usr/local/bin/run-code` -> `exec tsx main.ts` | Transpiles in memory with esbuild; **type errors are not reported** (tsx does not type-check). Block-list seccomp. |
| java | `eclipse-temurin:21.0.5_11-jdk-noble` | `java -XX:+UseSerialGC -XX:TieredStopAtLevel=1 -Xss8m Main.java` | JEP 330 source launch; the public class should be `Main`. C1-only JIT and serial GC to keep start-up and RSS low. |
| go | `golang:1.22.10-bookworm` | `run-code` -> copies a pre-warmed `GOCACHE` into `/build/.cache`, `go run` | Warm cache avoids ~14 s stdlib recompilation (per Dockerfile comment; not re-measured here). Block-list seccomp. |
| ruby | `ruby:3.3.6-slim-bookworm` | `ruby --disable-gems main.rb` | |
| rust | `rust:1.83.0-slim-bookworm` | `run-code` -> `rustc -O --edition 2021 -o /build/prog` then exec | No cargo, no crates; std only. Block-list seccomp. |
| bash | `debian:bookworm-slim` | `bash main.sh` | Smallest limits: 128 MB, 32 PIDs. |
