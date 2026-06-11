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
  "timeout_seconds": 10,         // optional, clamped to [1, MAX_TIMEOUT_SECONDS] (default 30)
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
  "files": []                    // produced artifacts (presigned URLs), if any
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
- **Errors:** `400` validation, `401` unauthenticated, `403` forbidden/invalid token, `408`
  execution exceeded the sync window (use the async endpoint), `429` rate or concurrency limit,
  `503` backend unavailable.

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

Cancels a `PENDING` job or kills a `RUNNING` one (SIGKILLs the container).

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
| 401 | `unauthenticated` | No/badly-formed credentials and anonymous disabled. | Send a Bearer JWT or `X-API-Key`. |
| 403 | `forbidden` | Invalid token, unknown API key, or job owned by another user. | Re-authenticate; only access your own jobs. |
| 404 | `not-found` | Unknown job id or route. | Verify the id; it may have expired (results TTL ≈ 1h). |
| 408 | `sync-timeout` | Sync execution exceeded the server window. | Use `POST /v1/execute/async` and poll. |
| 409 | `already-terminal` | Cancelling a job that already finished. | Nothing to do. |
| 429 | `rate-limited` | Per-tier request rate exceeded. | Honour the `Retry-After` header; back off. |
| 429 | `concurrency-limit` | Too many concurrent jobs for your tier. | Wait for in-flight jobs to finish. |
| 503 | `service-unavailable` | Redis/Docker unavailable (circuit breaker open). | Retry with backoff; check `/v1/health`. |
| 500 | `internal-error` | Unexpected server error (details are logged, not returned). | Retry; report with the `x-request-id` header. |

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
