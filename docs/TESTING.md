# Testing guide

## Layers

| Layer | Location | Needs | What it proves | Count (this branch) |
|---|---|---|---|---|
| Python unit | `tests/unit/test_*.py` | nothing | Executor logic against a fake Docker (flags, streaming, timeout/cancel ladder, OOM mapping, truncation, cleanup), reaper parsing, seccomp profile construction, resource limits, registry, queue consumer bookkeeping, worker job lifecycle | 144 |
| TypeScript unit | `tests/unit/*.test.ts` | nothing | JWT, auth, config validation, rate limiter and quota Lua logic (against an in-memory Redis), validator, error handler, circuit breaker, job queue, tracing | 87 |
| API integration | `tests/integration/api_server.test.ts` | nothing (in-memory Redis + fake worker) | The real Express app and WebSocket server end to end: routing, auth, limits, job lifecycle, 4xx/5xx mapping | 31 |
| Docker integration | `tests/integration/test_executor_real_docker.py` | Docker, built runtime images, Redis | Each language runs; network blocked; fork/memory/disk/CPU bombs contained; containers and workdirs cleaned; stream reclaim; cancellation, SIGTERM reaches a handler-less program | 25 |
| Security | `tests/security/` | Docker, runtime images | 24 escape payloads must not print `ESCAPED` and a final check that the host stays responsive; 9 direct-syscall tests prove seccomp (not just missing binaries) denies `unshare(CLONE_NEWUSER)`, `io_uring_setup`, `userfaultfd`, `perf_event_open`, `keyctl`, `ptrace` | 25 + 9 |
| End-to-end | `tests/e2e/user_scenarios.test.ts` | a running compose stack | User journeys over HTTP/WS | 10 |
| Load | `tests/e2e/load_test.js` | k6 + stack | 100-500 virtual-user scenarios | not run (see BENCHMARKS) |
| Benchmarks | `benchmarks/` | Docker | See [BENCHMARKS.md](BENCHMARKS.md) | n/a |

The unit and API-integration tiers use `vitest` (API) and `pytest` (worker). The two tiers that
need Docker are marked `integration` and skip themselves when Docker or an image is missing
(`tests/conftest.py`: `require_language`).

## Running

```bash
make install                         # npm install + pip install
make lint                            # ruff, mypy (strict), eslint, tsc
make test-unit                       # pytest tests/unit + vitest (no services)
make build-runtimes                  # once
REDIS_URL=redis://localhost:6379/15 make test-integration    # real Docker + Redis
make test-e2e                        # `cd api && vitest run --config vitest.e2e.config.ts`; against a running
                                     # `make dev` stack (set API_TOKEN if anonymous is off); skips if the API is down
make test-coverage                   # unit tests with the CI coverage gates
```

On a small machine limit parallelism: `cd api && npx vitest run --poolOptions.forks.maxForks=2
--poolOptions.forks.minForks=1` (the `--maxWorkers` flag trips a tinypool check in vitest 2.1.8
with the default fork pool). pytest runs serially.

CI (`.github/workflows/ci.yml`): lint API, lint worker (ruff check, ruff format --check, mypy),
vitest with coverage gate, pytest with coverage gate, build all images, then integration +
security against a Redis service container.

## Coverage gates

* Worker: `--cov=worker.sandbox --cov-fail-under=90`. Only `worker/sandbox` is measured; the
  daemon (`worker/worker.py`), consumer and result store are outside the gate (this branch added
  unit tests for the daemon and consumer bookkeeping, but they do not count toward it).
* API: lines/statements/functions >= 90%, branches >= 85% over `src/**` except `index.ts`
  (bootstrap) and `services/websocket.ts` (exercised by the integration test instead).
* Measured at the end of this branch: see the table in [BENCHMARKS.md](BENCHMARKS.md#test-results).

## Test design notes

* **FakeDocker** (`tests/conftest.py`) implements the executor's two seams (`spawn`, `simple`):
  process objects that exit, hang until killed, or ignore `docker kill` (new `ignore_kill`), and a
  daemon that can be down or missing the image. This is how timeout, cancel and OOM paths are tested
  deterministically.
* **FakeRedis** (`tests/helpers/fakeRedis.ts`) re-implements the commands the API uses, including
  the two Lua scripts, so admission logic is tested without a server. It has a `failing` switch to
  simulate outages.
* **Escape payloads print a sentinel** only if the dangerous action succeeds, so assertions are
  precise. Weakness identified by the audit: many payloads shell out to tools absent from the images
  (`mount`, `modprobe`), which would pass even with no seccomp. `test_seccomp_enforced.py`
  addresses that for the syscalls where the result differs with and without the filter.
* **Regression tests added in this branch**: unkillable container does not hang the worker
  (`test_executor.py`), poison and malformed messages reach a terminal state, acked entries are
  deleted, dead-letter capped, slots released, consume loop bounded by free slots
  (`test_worker_daemon.py`), fresh `Created` containers not reaped (`test_cleanup.py`), startup
  metric observed, async errors forwarded and mapped to 503/400, XFF cannot mint buckets,
  WebSocket obeys the rate limit and concurrency quota, API-key identities distinct, `host:port`
  MinIO endpoint accepted, multi-byte output split across reads decoded intact, unmatched routes
  do not create metric labels, sandboxes run under `--init` and SIGTERM reaches a handler-less
  program (real container).

## Not covered

* The worker's real Redis loop (`WorkerDaemon.run`) with a real Redis and real Docker together: the
  pieces are tested separately and by the benchmark stack, not by an automated test.
* `index.ts` shutdown path; MinIO interactions (mocked); Docker daemon outage in the middle of a
  real run; multi-worker reclaim under load (the single-reclaim case is covered).
* Non-x86_64 hosts: the seccomp-enforcement tests skip themselves.
* Kernel-level escapes. These tests show known techniques are blocked, not that none exist.
