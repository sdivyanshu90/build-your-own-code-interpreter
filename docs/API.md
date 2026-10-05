# API Reference

Base URL: `https://<host>/v1` (local dev: `http://localhost:8080/v1`).
All request/response bodies are JSON. Errors use [RFC 7807 Problem Details](https://www.rfc-editor.org/rfc/rfc7807)
with `Content-Type: application/problem+json`.

---

## Authentication

Every endpoint except `GET /v1/languages`, `GET /v1/health`, and `GET /v1/metrics` requires a
principal. Provide **one** of:

| Method | Header | Notes |
|--------|--------|-------|
| JWT bearer | `Authorization: Bearer <token>` | HS256, signed with the server's `JWT_SECRET`. Claims: `sub` (required), `tier` (`authenticated` \| `premium`). |
| API key | `X-API-Key: <key>` | Machine clients. Keys are configured server-side as `API_KEYS=<key>:<tier>,…`. |
| Anonymous | _none_ | Allowed only when `ALLOW_ANONYMOUS=true` (dev). Treated as the `anonymous` tier. |

Tiers control rate limits and concurrency (anonymous < authenticated < premium).

**Minting a dev JWT** (HS256, matching the in-house verifier):

```python
import base64, hashlib, hmac, json, time

def sign(secret: str, sub: str, tier: str = "authenticated") -> str:
    def b64(b: bytes) -> str: return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": sub, "tier": tier, "iat": int(time.time()),
                           "exp": int(time.time()) + 3600}).encode())
    sig = b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"

print(sign("your-strong-jwt-secret-at-least-32-chars", "user-123"))
```

---

## Data model

### `ExecutionRequest`

```jsonc
{
  "language": "python",          // one of: python javascript typescript java go ruby rust bash
  "code": "print('hello')",      // required, 1..MAX_CODE_SIZE_BYTES (default 256 KiB)
  "stdin": "",                   // optional, ..MAX_STDIN_BYTES
  "timeout_seconds": 10,         // optional positive int; default DEFAULT_TIMEOUT_SECONDS (10);
                                 // clamped to MAX_TIMEOUT_SECONDS (30), then again by the worker to
                                 // the language ceiling (python 10 s ... rust 25 s)
  "env_vars": { "KEY": "VALUE" },// optional; dangerous names (PATH, LD_*, …) are stripped
  "files": [                     // optional read-only files mounted next to the code
    { "name": "data.csv", "content": "1,2,3" }   // names are sanitised to a safe basename
  ]
}
```

### `ExecutionResult`

```jsonc
{
  "job_id": "01J9Z8...",         // ULID
  "status": "COMPLETED",         // PENDING|RUNNING|COMPLETED|FAILED|TIMEOUT|KILLED
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,                // null if killed before exit
  "wall_time_ms": 142,
  "cpu_time_ms": 31,
  "memory_bytes": 9437184,       // cgroup peak
  "oom_killed": false,
  "timed_out": false,
  "truncated": false,            // true if output hit output_max_bytes (default 1 MiB)
  "files": []                    // reserved for produced artifacts; currently always empty
}
```

> **Status semantics:** a program that exits non-zero is still `COMPLETED` (we captured its
> result). `FAILED` is reserved for infrastructure/sandbox failures and OOM kills. `TIMEOUT` means
> the wall-clock limit fired; `KILLED` means the user cancelled it.

---

## Endpoints

### `POST /v1/execute` — synchronous execution

Runs the code and blocks (long-poll) until it finishes or the synchronous window elapses.

- **Auth:** required. **Rate limit:** per tier.
- **Request:** an `ExecutionRequest`.
- **`200`** → an `ExecutionResult`.
- **Errors:** `400` validation or malformed JSON, `401` no credentials, `403` invalid token /
  unknown API key, `408` execution exceeded the sync window (the job keeps running; the body has
  `job_id`), `413` body too large, `429` rate or concurrency limit, `503` Redis unavailable.

```bash
curl -s -X POST http://localhost:8080/v1/execute \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"language":"python","code":"print(6*7)"}'
# {"job_id":"01J...","status":"COMPLETED","stdout":"42\n","exit_code":0,...}
```

```python
import requests
r = requests.post("http://localhost:8080/v1/execute",
                  headers={"Authorization": f"Bearer {TOKEN}"},
                  json={"language": "python", "code": "print(6*7)"})
print(r.json()["stdout"])  # "42\n"
```

```javascript
const res = await fetch("http://localhost:8080/v1/execute", {
  method: "POST",
  headers: { "Authorization": `Bearer ${TOKEN}`, "Content-Type": "application/json" },
  body: JSON.stringify({ language: "python", code: "print(6*7)" }),
});
console.log((await res.json()).stdout); // "42\n"
```

### `POST /v1/execute/async` — submit, return immediately

- **Auth:** required. **Request:** an `ExecutionRequest`.
- **`202`** → `{ "job_id": "01J...", "status": "PENDING", "poll_url": "/v1/jobs/01J..." }`.
- **Errors:** `400`, `401`, `429`.

```bash
curl -s -X POST http://localhost:8080/v1/execute/async \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"language":"go","code":"package main\nimport \"fmt\"\nfunc main(){fmt.Println(42)}"}'
```

### `GET /v1/jobs/{job_id}` — poll status / result

- **Auth:** required (owner-scoped). **`200`** → a `JobRecord` (includes `result` once terminal).
- **Errors:** `400` malformed id, `403` belongs to another user, `404` unknown.

```bash
curl -s http://localhost:8080/v1/jobs/01J... -H "Authorization: Bearer $TOKEN"
```

```python
import time, requests
def run_async(req):
    job = requests.post(f"{BASE}/v1/execute/async", headers=H, json=req).json()
    while True:
        rec = requests.get(f"{BASE}/v1/jobs/{job['job_id']}", headers=H).json()
        if rec["status"] in ("COMPLETED", "FAILED", "TIMEOUT", "KILLED"):
            return rec["result"]
        time.sleep(0.5)
```

### `DELETE /v1/jobs/{job_id}` — cancel

Cancels a `PENDING` job (skipped when dequeued) or kills a `RUNNING` one (the worker polls the
cancel flag every 0.5 s, then sends SIGTERM, waits `SIGTERM_GRACE_SECONDS`, then SIGKILL). The
record is rewritten to `KILLED` immediately; the worker later stores the authoritative result.

- **`200`** → `{ "job_id": "01J...", "status": "KILLED" }`.
- **Errors:** `403`, `404`, `409` (already terminal).

```bash
curl -s -X DELETE http://localhost:8080/v1/jobs/01J... -H "Authorization: Bearer $TOKEN"
```

### `GET /v1/languages` — list runtimes

- **Auth:** none. **`200`** →

```jsonc
{ "languages": [
  { "id": "python", "name": "Python", "version": "3.12",
    "default_timeout_seconds": 10, "memory_mb": 256 }, ...
] }
```

### `GET /v1/health` — liveness/readiness

- **Auth:** none. **`200`** when Redis is reachable, **`503`** otherwise.

```jsonc
{ "status": "ok", "redis": "up", "minio": "up", "uptime_seconds": 1234, "version": "1.0.0" }
```

### `GET /v1/metrics` — Prometheus exposition

- **Auth:** none (network-restricted in production). **`200`** `text/plain; version=0.0.4`.

---

## WebSocket — `WS /v1/execute/stream`

Real-time stdout/stderr streaming.

- **Upgrade auth:** `Authorization`/`X-API-Key` header, or `?token=<jwt>` query param for browser
  clients that cannot set headers. A failed auth is rejected with `401` before the WS handshake.
- **Liveness:** server pings every 15 s; a connection missing two pongs is closed.
- **Limits:** the stream path applies the same per-user rate limit and concurrency quota as
  `POST /v1/execute` (error frames titled `Too Many Requests` / `Concurrency Limit`). Hard stream
  lifetime 120 s; max inbound frame 1 MiB.
- **Backpressure:** if the client cannot keep up (send buffer over the high-water mark), the server
  sends an `error` frame and closes.

### Protocol

Client → server (exactly one frame, then listen):

```jsonc
{ "type": "start", "request": { /* ExecutionRequest */ } }
```

Server → client frames:

| `type` | Payload | Meaning |
|--------|---------|---------|
| `accepted` | `{ job_id }` | Job enqueued; streaming begins. |
| `status` | `{ status }` | Lifecycle transition (e.g. `RUNNING`). |
| `stdout` | `{ data }` | A chunk of standard output. |
| `stderr` | `{ data }` | A chunk of standard error. |
| `exit` | `{ exit_code, status, wall_time_ms, timed_out, oom_killed }` | Terminal; server closes after this. |
| `error` | `{ title, detail }` | Validation/backpressure/timeout error; server closes. |

```javascript
import WebSocket from "ws";
const ws = new WebSocket(`ws://localhost:8080/v1/execute/stream?token=${TOKEN}`);
ws.on("open", () => ws.send(JSON.stringify({
  type: "start",
  request: { language: "python", code: "import time\nfor i in range(3):\n print(i); time.sleep(0.5)" },
})));
ws.on("message", (d) => {
  const f = JSON.parse(d.toString());
  if (f.type === "stdout") process.stdout.write(f.data);
  if (f.type === "exit") { console.log("exit", f.exit_code); ws.close(); }
});
```

---

## Error catalogue

All errors are `application/problem+json`:

```jsonc
{ "type": "https://docs.sandbox.local/errors/validation-error",
  "title": "Validation Error", "status": 400,
  "detail": "The request body failed validation.",
  "instance": "/v1/execute", "code": "validation-error",
  "errors": [ { "field": "language", "message": "language must be one of: …" } ] }
```

| Status | `code` | Cause | Resolution |
|--------|--------|-------|-----------|
| 400 | `validation-error` | Unknown language, empty/oversized code, bad timeout, unsafe filename, oversized env value. | Inspect `errors[]`; fix the offending field. |
| 400 | _(invalid job id)_ | Job id is not a valid ULID. | Use the `job_id` returned by submit. |
| 401 | _(type slug only)_ `unauthenticated` | No credentials and anonymous disabled. | Send a Bearer JWT or `X-API-Key`. |
| 403 | _(type slug only)_ `forbidden` | Invalid token (bad signature, expired, missing `sub`), unknown API key, or job owned by another user. | Re-authenticate; only access your own jobs. |
| 404 | _(type slug only)_ `not-found` | Unknown job id or route. | Verify the id; records expire after 2 h. |
| 408 | `sync-timeout` | Sync execution exceeded the server window. | Use `POST /v1/execute/async` and poll. |
| 409 | `already-terminal` | Cancelling a job that already finished. | Nothing to do. |
| 429 | `rate-limited` | Per-tier request rate exceeded. | Honour the `Retry-After` header; back off. |
| 429 | `concurrency-limit` | Too many concurrent jobs for your tier. | Wait for in-flight jobs to finish. |
| 503 | `service-unavailable` | Redis unreachable or the Redis circuit breaker is open (`Retry-After: 5`). Docker problems do **not** surface here: the job stays pending/running and eventually becomes `FAILED`. | Retry with backoff; check `/v1/health`. |
| 400/413 | `validation-error` | Malformed JSON or body larger than `MAX_CODE_SIZE + MAX_STDIN + 64 KiB`. | Fix the payload. |
| 500 | _(type slug only)_ `internal-error` | Unexpected server error (details are logged, not returned). | Retry; report with the `x-request-id` header. |

Only errors raised with an explicit `code` carry a `code` property in the body; for the others the
last path segment of `type` is the stable identifier.

Rate-limited responses include `Retry-After` (seconds) and `RateLimit-Limit` / `RateLimit-Remaining`.

---

## Supported languages

| id | Name | Version | Default timeout | Memory |
|----|------|---------|-----------------|--------|
| `python` | Python | 3.12 | 10 s | 256 MB |
| `javascript` | JavaScript (Node.js) | 20 | 10 s | 256 MB |
| `typescript` | TypeScript | 5.7 | 15 s | 320 MB |
| `java` | Java | 21 | 20 s | 512 MB |
| `go` | Go | 1.22 | 20 s | 384 MB |
| `ruby` | Ruby | 3.3 | 10 s | 256 MB |
| `rust` | Rust | 1.83 | 25 s | 512 MB |
| `bash` | Bash | 5.2 | 10 s | 128 MB |

See [`ADDING_LANGUAGE.md`](./ADDING_LANGUAGE.md) to add more.

---

## Captured examples

Real responses from the benchmark stack (commit recorded in
`benchmarks/results/smoke_examples.json`, produced by `benchmarks/smoke_examples.py`, 2026-10-04).
Job ids and timings vary per run; the shapes do not.

**sync python** - `POST /v1/execute {"language":"python","code":"print(6*7)"}` -> `200`

```json
{
  "job_id": "01M43W5Z4EF3D0TCC8HB6FXS3H",
  "status": "COMPLETED",
  "stdout": "42\n",
  "stderr": "",
  "exit_code": 0,
  "wall_time_ms": 1017,
  "cpu_time_ms": 100,
  "memory_bytes": 13856768,
  "oom_killed": false,
  "timed_out": false,
  "truncated": false,
  "files": []
}
```
**nonzero exit** - `POST /v1/execute` with `import sys; print("x"); sys.exit(3)` - a non-zero exit is still `COMPLETED` -> `200`

```json
{
  "job_id": "01M43W61FSXJX11F3V9EWZFW4G",
  "status": "COMPLETED",
  "stdout": "x\n",
  "stderr": "",
  "exit_code": 3,
  "wall_time_ms": 729,
  "cpu_time_ms": 114,
  "memory_bytes": 5124096,
  "oom_killed": false,
  "timed_out": false,
  "truncated": false,
  "files": []
}
```
**syntax error** - `POST /v1/execute` with `def (` - interpreter errors arrive on `stderr` with `COMPLETED` -> `200`

```json
{
  "job_id": "01M43W62CSG5RQM18ZV6025ED0",
  "status": "COMPLETED",
  "stdout": "",
  "stderr": "  File \"/sandbox/main.py\", line 1\n    def (\n        ^\nSyntaxError: invalid syntax\n",
  "exit_code": 1,
  "wall_time_ms": 518,
  "cpu_time_ms": 38,
  "memory_bytes": 4911104,
  "oom_killed": false,
  "timed_out": false,
  "truncated": false,
  "files": []
}
```
**stdin and files** - `POST /v1/execute` with `stdin: "abc\n"` and `files: [{name: "data.txt", ...}]` (input files are at `/sandbox/<name>`) -> `200`

```json
{
  "job_id": "01M43W6337TVJKWH732BHD3ZS5",
  "status": "COMPLETED",
  "stdout": "abc from file\n",
  "stderr": "",
  "exit_code": 0,
  "wall_time_ms": 587,
  "cpu_time_ms": 75,
  "memory_bytes": 4874240,
  "oom_killed": false,
  "timed_out": false,
  "truncated": false,
  "files": []
}
```
**timeout job** - `POST /v1/execute` with `timeout_seconds: 2` and `while True: pass` - HTTP 200, `status: TIMEOUT`, `exit_code: null` -> `200`

```json
{
  "job_id": "01M43W63SJE4ARYTAS8HP4XTZ9",
  "status": "TIMEOUT",
  "stdout": "",
  "stderr": "",
  "exit_code": null,
  "wall_time_ms": 2275,
  "cpu_time_ms": 938,
  "memory_bytes": 6656000,
  "oom_killed": false,
  "timed_out": true,
  "truncated": false,
  "files": []
}
```
**async submit** - `POST /v1/execute/async` -> `202`

```json
{
  "job_id": "01M43W60HRM14JBQ26TG56SFZH",
  "status": "PENDING",
  "poll_url": "/v1/jobs/01M43W60HRM14JBQ26TG56SFZH"
}
```
**job record terminal** - `GET /v1/jobs/{id}` after completion (a `JobRecord`: the request, the worker that ran it, and the result) -> `200`

```json
{
  "job_id": "01M43W60HRM14JBQ26TG56SFZH",
  "user_id": "example-user",
  "language": "bash",
  "status": "COMPLETED",
  "request": {
    "language": "bash",
    "code": "echo async",
    "timeout_seconds": 10
  },
  "created_at": "2026-10-04T16:33:03.033Z",
  "updated_at": "2026-10-04T16:33:03.976311+00:00",
  "retries": 0,
  "worker_id": "fc5031193210-a9b4abce",
  "result": {
    "job_id": "01M43W60HRM14JBQ26TG56SFZH",
    "status": "COMPLETED",
    "stdout": "async\n",
    "stderr": "",
    "exit_code": 0,
    "wall_time_ms": 808,
    "cpu_time_ms": 44,
    "memory_bytes": 4907008,
    "oom_killed": false,
    "timed_out": false,
    "truncated": false,
    "files": []
  }
}
```
**validation error** - `POST /v1/execute` with an unknown language -> `400`

```json
{
  "type": "https://docs.sandbox.local/errors/validation-error",
  "title": "Validation Error",
  "status": 400,
  "detail": "The request body failed validation.",
  "instance": "/execute",
  "code": "validation-error",
  "errors": [
    {
      "field": "language",
      "message": "language must be one of: python, javascript, typescript, java, go, ruby, rust, bash"
    }
  ]
}
```
**no credentials** - `POST /v1/execute` without credentials (anonymous disabled) -> `401`

```json
{
  "type": "https://docs.sandbox.local/errors/unauthenticated",
  "title": "Authentication required",
  "status": 401,
  "detail": "Provide a Bearer JWT in Authorization or a key in X-API-Key.",
  "instance": "/execute"
}
```
**bad token** - `POST /v1/execute` with a malformed bearer token -> `403`

```json
{
  "type": "https://docs.sandbox.local/errors/forbidden",
  "title": "Invalid token",
  "status": 403,
  "detail": "JWT verification failed: invalid header",
  "instance": "/execute"
}
```
**cancel terminal job** - `DELETE /v1/jobs/{id}` on a finished job -> `409`

```json
{
  "type": "https://docs.sandbox.local/errors/already-terminal",
  "title": "Conflict",
  "status": 409,
  "detail": "Job is already COMPLETED.",
  "instance": "/jobs/01M43W60HRM14JBQ26TG56SFZH",
  "code": "already-terminal"
}
```
**malformed json** - `POST /v1/execute` with body `{not json` -> `400`

```json
{
  "type": "https://docs.sandbox.local/errors/validation-error",
  "title": "Bad Request",
  "status": 400,
  "detail": "The request body could not be parsed.",
  "instance": "/v1/execute",
  "code": "validation-error"
}
```
Notes from the captures:

* `instance` is `req.path` as seen by the router, so for errors raised inside the `/v1` routers it
  is mount-relative (`/execute`, `/jobs/<id>`), while errors raised by the global handler use the
  full path (`/v1/execute`). Clients should not parse it.
* `memory_bytes` and `cpu_time_ms` come from the worker's cgroup sampler (50 ms interval); they are
  `0` if the container is too short-lived or the cgroup path is not visible to the worker.
* After these jobs finished, `XLEN sandbox:jobs` was `0`, `XPENDING` was `0` and the user's
  concurrency set had `0` members (acked entries are deleted and slots released by the worker).
