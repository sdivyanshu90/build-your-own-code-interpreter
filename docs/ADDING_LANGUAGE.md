# Adding a New Language Runtime

This guide explains how to add a new language runtime to the Code Interpreter
Sandbox, end to end, with a fully worked example: **Lua 5.4**.

It is the operational runbook referenced from `api/src/languages.ts`. Follow the
numbered checklist exactly — every step keys off the single `Language` union, and
skipping one (especially the resource-defaults entry) breaks the registry build.

---

## 1. Registry architecture

There is **one source of truth** for the set of valid language ids: the `LANGUAGES`
tuple in `api/src/types/index.ts`.

```ts
export const LANGUAGES = [
  'python', 'javascript', 'typescript', 'java', 'go', 'ruby', 'rust', 'bash',
] as const;

export type Language = (typeof LANGUAGES)[number];
```

Four subsystems key off that union. A new language must be wired into **all four**,
plus its runtime image, or it will either fail type-checking, fail to build the
worker registry, or run with no sandbox profile:

| Subsystem | File | Role |
| --- | --- | --- |
| API metadata | `api/src/languages.ts` | The `RUNTIMES: Record<Language, RuntimeInfo>` map served by `GET /v1/languages` and used for request validation. |
| Execution registry | `worker/sandbox/image_registry.py` | The authoritative map of language → image, entrypoint argv, source filename, and `needs_exec_build`. |
| Resource defaults | `worker/sandbox/resource_limits.py` | The `_DEFAULTS: dict[str, ResourceConfig]` envelope (memory, CPU, timeout, PID cap, etc.). |
| Seccomp builder | `worker/sandbox/seccomp.py` | Generates the default-deny syscall allow-list. Static profiles live in `runtimes/<lang>/seccomp-profile.json` and are *generated*, never hand-edited. |

The runtime image itself lives in `runtimes/<lang>/Dockerfile` (built as
`sandbox-runtime-<lang>:latest`) with its committed profile at
`runtimes/<lang>/seccomp-profile.json`.

Because `image_registry.py` calls `get_resource_config(language)` while building the
registry **at import time**, a missing `_DEFAULTS` entry raises `KeyError` and the
worker fails to start. The `RUNTIMES` record in `languages.ts` is typed as
`Record<Language, RuntimeInfo>`, so a missing entry there is a compile error. The
TypeScript union and the Python dicts are kept in sync only by this checklist.

---

## 2. Checklist

1. **Create the Dockerfile** at `runtimes/<lang>/Dockerfile` — pinned minimal base,
   dangerous binaries removed, `USER 65534:65534`, `WORKDIR /tmp`, **no `ENTRYPOINT`**.
2. **Add seccomp behavior** in `worker/sandbox/seccomp.py` (`LANGUAGE_EXTRA`) — only if
   the runtime needs syscalls beyond `COMMON_SYSCALLS`; most interpreted languages need
   none.
3. **Register the runtime** in `worker/sandbox/image_registry.py` (`specs` list in
   `_build()`).
4. **Add resource defaults** in `worker/sandbox/resource_limits.py` (`_DEFAULTS`).
   *Mandatory* — the registry build fails without it.
5. **Add the id to the API union + metadata**: `api/src/types/index.ts` (`LANGUAGES`)
   and `api/src/languages.ts` (`RUNTIMES`).
6. **Regenerate seccomp + build + scan**: `make seccomp-regen`, `make build-runtimes`,
   `make security-scan`.
7. **Add tests**: the id to the `LANGUAGES` list in `tests/unit/test_seccomp.py`, to the
   loops in `tests/unit/test_resource_limits.py`, and a smoke + isolation test in
   `tests/integration/test_executor_real_docker.py`.
8. **Update the docs tables** in `docs/API.md` and `README.md`.

Also add the language to the `LANGS` list in the `Makefile` so `build-runtimes`,
`security-scan`, and `seccomp-regen` iterate over it.

---

## 3. Worked example: Lua 5.4

Lua is an interpreted language. The worker runs `lua /sandbox/main.lua` directly: no
compile step, no writable build dir (`needs_exec_build=False`), and no extra syscalls
beyond `COMMON_SYSCALLS`.

### 3.1 `runtimes/lua/Dockerfile`

Debian `bookworm-slim` ships the `lua5.4` package and gives us the standard hardening
surface used by the bash/ruby runtimes. We install the interpreter, symlink it to
`lua`, then strip every privilege-escalation and network/transfer binary. No
`ENTRYPOINT` — the worker passes the full run argv.

```dockerfile
# syntax=docker/dockerfile:1.7
# Lua 5.4 sandbox runtime on a minimal Debian base. The worker runs `lua /sandbox/main.lua`
# directly (interpreted, no compile step, /tmp stays noexec). This runtime pairs with a
# scripting-language resource envelope and the COMMON_SYSCALLS seccomp profile (no extras).
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM debian:bookworm-slim

# Install the Lua 5.4 interpreter, then expose it as `lua`.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends lua5.4; \
    ln -sf /usr/bin/lua5.4 /usr/local/bin/lua; \
    apt-get purge -y --auto-remove; \
    rm -rf /var/lib/apt/lists/*

# Harden: remove privilege-escalation, network/transfer, and package-management binaries.
# A Lua script should be able to do computation and string processing, nothing more.
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp tftp ssh scp sftp rsync \
               chmod chown apt apt-get dpkg; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" \
            "/usr/local/bin/$bin" 2>/dev/null || true; \
    done

# `nobody` (65534) exists on Debian.
USER 65534:65534
WORKDIR /tmp
CMD ["lua", "-v"]
```

`CMD` (a debug-only `lua -v`) is fine; it is **not** an `ENTRYPOINT`, so the worker's
run argv replaces it.

### 3.2 `api/src/types/index.ts` — add `'lua'` to `LANGUAGES`

```ts
export const LANGUAGES = [
  'python',
  'javascript',
  'typescript',
  'java',
  'go',
  'ruby',
  'rust',
  'bash',
  'lua',
] as const;
```

### 3.3 `api/src/languages.ts` — add the Lua `RuntimeInfo`

Add a `lua` entry to the `RUNTIMES` record (placed after `bash`):

```ts
  bash: {
    id: 'bash',
    name: 'Bash',
    version: '5.2',
    default_timeout_seconds: 10,
    memory_mb: 128,
  },
  lua: {
    id: 'lua',
    name: 'Lua',
    version: '5.4',
    default_timeout_seconds: 10,
    memory_mb: 128,
  },
```

### 3.4 `worker/sandbox/image_registry.py` — add the Lua spec

Append a dict to the `specs` list inside `_build()`. The image name
(`sandbox-runtime-lua:<tag>`) and any pinned digest (`SANDBOX_DIGEST_LUA`) are derived
automatically by `_image_for("lua")`.

```python
        {
            "id": "lua",
            "name": "Lua",
            "version": "5.4",
            "entrypoint": ["lua"],
            "source_filename": "main.lua",
            "needs_exec_build": False,
        },
```

At runtime the executor builds the argv via `argv_for(source_path)`, producing
`["lua", "/sandbox/main.lua"]`.

### 3.5 `worker/sandbox/resource_limits.py` — add Lua to `_DEFAULTS`

Lua is a lightweight scripting runtime, so it gets a small envelope — comparable to the
bash entry, with a slightly higher PID limit since the interpreter is less fork-happy
than a shell.

```python
    "lua": ResourceConfig(
        # Lightweight scripting runtime: small footprint, no compile step.
        memory_mb=128,
        cpu_quota=0.5,
        timeout_seconds=10,
        pids_limit=64,
        disk_mb=64,
        network_enabled=False,
        output_max_bytes=1_048_576,
    ),
```

> **Do not skip this step.** `_build()` in `image_registry.py` calls
> `get_resource_config("lua")` at import time; without the `_DEFAULTS` entry it raises
> `KeyError` and the worker fails to start.

### 3.6 `worker/sandbox/seccomp.py` — declare Lua's syscall extras

Lua needs nothing beyond `COMMON_SYSCALLS`. Add an explicit empty entry to
`LANGUAGE_EXTRA` so the language is documented and `allowed_syscalls("lua")` is
unambiguous (`LANGUAGE_EXTRA.get` would otherwise fall back to `frozenset()` anyway,
but listing it keeps the table complete):

```python
LANGUAGE_EXTRA: Final[dict[str, frozenset[str]]] = {
    "python": frozenset(),
    "javascript": frozenset({"epoll_pwait", "statx"}),
    "typescript": frozenset({"epoll_pwait", "statx"}),
    "java": frozenset({"get_mempolicy", "set_mempolicy", "getcpu", "sched_setaffinity"}),
    "go": frozenset({"sched_getaffinity", "rseq"}),
    "ruby": frozenset(),
    "rust": frozenset(),
    "lua": frozenset(),
}
```

### 3.7 Regenerate the seccomp profile

The committed `runtimes/lua/seccomp-profile.json` is generated by the builder, never
written by hand. Regenerate all profiles (recommended, and required if you touched the
builder):

```bash
make seccomp-regen
```

Or regenerate just the Lua profile:

```bash
PYTHONPATH=. python -m worker.sandbox.seccomp lua > runtimes/lua/seccomp-profile.json
```

The output has `defaultAction: SCMP_ACT_ERRNO`, the `COMMON_SYSCALLS` allow-list, the
masked `clone` rule, the `clone3`→`ENOSYS` rule, with `DANGEROUS_SYSCALLS` subtracted.

> After editing `seccomp.py` you **must** regenerate, or the
> `test_static_artifacts_match_builder_output` test will fail (see Troubleshooting).

### 3.8 Add tests

**`tests/unit/test_seccomp.py`** — add `"lua"` to the module-level `LANGUAGES` list:

```python
LANGUAGES = ["python", "javascript", "typescript", "java", "go", "ruby", "rust", "bash", "lua"]
```

**`tests/unit/test_resource_limits.py`** — add `"lua"` to the iteration tuples (for
example in `test_network_disabled_by_default`):

```python
    def test_network_disabled_by_default(self):
        for lang in ("python", "javascript", "java", "go", "ruby", "rust", "bash", "typescript", "lua"):
            assert get_resource_config(lang).network_enabled is False
```

**`tests/integration/test_executor_real_docker.py`** — add a smoke test and an isolation
test, matching the style of the existing per-language tests:

```python
    async def test_lua_5_4_arithmetic(self, real_executor, require_language):
        require_language("lua")
        result = await _run(real_executor, "lua", "print(6*7)")
        assert result.status == "COMPLETED"
        assert result.stdout.strip() == "42"
        assert result.exit_code == 0

    async def test_lua_cannot_open_socket(self, real_executor, require_language):
        require_language("lua")
        # Lua's stock interpreter has no socket library; os.execute is also unavailable
        # because the network/transfer binaries were stripped from the image.
        result = await _run(real_executor, "lua", 'os.execute("wget http://example.com")')
        assert "CONNECTED" not in result.stdout
```

### 3.9 Update the Makefile language list

```make
LANGS := python javascript typescript java go ruby rust bash lua
```

This makes `build-runtimes`, `security-scan`, `seccomp-regen`, and `push-runtimes`
include Lua.

---

## 4. Verify it works

Build the runtime images and scan them (Trivy fails on `HIGH`/`CRITICAL`):

```bash
make build-runtimes && make security-scan
```

Run the unit + integration suites:

```bash
make test-unit
make test-integration   # requires Docker + the sandbox-runtime-lua image
```

Then exercise the running API end to end:

```bash
curl -sS -X POST http://localhost:8080/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"language":"lua","code":"print(6*7)"}'
```

Expected (abbreviated) response — `stdout` is `"42"` and the job completes:

```json
{
  "job_id": "…",
  "status": "COMPLETED",
  "stdout": "42\n",
  "stderr": "",
  "exit_code": 0,
  "timed_out": false,
  "oom_killed": false
}
```

`GET /v1/languages` should now list Lua:

```bash
curl -sS http://localhost:8080/v1/languages | jq '.[] | select(.id=="lua")'
```

---

## 5. Troubleshooting

- **`tests/unit/test_seccomp.py::test_static_artifacts_match_builder_output` fails.**
  The committed `runtimes/<lang>/seccomp-profile.json` drifted from the builder output —
  usually because you edited `seccomp.py` but forgot to regenerate. Run
  `make seccomp-regen` and commit the updated profiles.

- **The worker fails to start / the registry raises `KeyError` at import.** You added the
  language to `image_registry.py` but forgot the `_DEFAULTS` entry in
  `resource_limits.py`. `_build()` calls `get_resource_config(<lang>)` for every spec, so
  every registered language must have resource defaults.

- **TypeScript compile error on `RUNTIMES`.** `RUNTIMES` is typed
  `Record<Language, RuntimeInfo>`; once the id is in the `LANGUAGES` union, you must add
  the matching entry in `languages.ts`.

- **Trivy fails the scan.** Pin a newer patch tag of the base image (or the apt package)
  and rebuild; the build is reproducible from `runtimes/<lang>/Dockerfile`.
