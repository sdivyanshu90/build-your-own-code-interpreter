# Benchmarks

Measured behaviour of this implementation: per-language latency (cold and warm), end-to-end API
latency, throughput under concurrency, the cost of the hardening versus a bare `docker run`,
timeout-enforcement accuracy, memory footprint, and the security test results. Every number below
is copied from `benchmarks/results/*.json` by `benchmarks/make_report.py`; nothing is estimated.
Methodology, commands and caveats come first because the numbers are only meaningful with them.

## Methodology

**Hardware and software.** Single laptop, shared with other workloads (see "Limitations"):
11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz, 8 logical CPUs, 5928 MB RAM, kernel 6.18.40.1-microsoft-standard-WSL2, Docker 29.4.3 (cgroup 2 systemd), Python 3.12.13.

**Date.** 2026-10-04 (all runs). Commits: all runs were taken on this branch (stack images were built from the working tree at the time);
the commit recorded inside each JSON is the one that was checked out when that run started. The
"before `--init`" runs use commit `5ca442f` (the audit's other fixes already applied); the "after"
runs include `2ba4387`.

**Harness.** `benchmarks/` (see its README for the exact commands). Two tiers:

1. *Executor tier* (`bench_executor.py`): drives `SandboxExecutor` directly against real Docker.
   No Redis, no API. Isolates the sandbox engine.
2. *Stack tier* (`bench_api.py`): `docker compose` with Redis, MinIO, the API and one worker
   (`WORKER_CONCURRENCY=4`), every container memory-capped through
   `benchmarks/docker-compose.bench.yml` (redis 128 MB, minio 384 MB, api 256 MB, worker 256 MB), per-user
   rate and concurrency limits lifted so the quota does not mask the pipeline. Load is a stdlib
   closed-loop generator (`http.client` threads), run on the same machine as the system under test.

**Statistics.** Nearest-rank percentiles over the raw samples (every reported percentile is an
observed value). Cell sizes are small (10-30), so p95/p99 equal the largest or second-largest
sample: read them as "worst observed", not as a stable estimate. Raw samples are in the `*_raw.csv`
files.

**Cold vs warm.** *Cold* is the first execution of a language in a fresh benchmark process; *warm*
is every subsequent one. It is not a true cold start: evicting the OS page cache requires root, so
image layers may already be cached, and the Docker daemon is already running. What "cold" does
include is seccomp-profile generation and first-use effects inside the process. Because every
execution is a brand-new container, "warm" has no meaning for the sandbox itself (there is no
container reuse); the gap between cold and warm is therefore small by construction.

**What the timers include.** `total` = `perf_counter` around `SandboxExecutor.execute`
(`docker image inspect`, work-dir creation, `docker run`, streaming, cgroup sampling, `docker rm
-f`, cleanup). `container` = the executor's own spawn-to-exit measurement (`wall_time_ms` in the
API result): container creation + program + teardown, excluding the pre- and post-steps.

## Results

### Test results

`bench_security.py` (`tests/security` + `tests/integration`, real containers): **59 passed, 0 failed, 0 errors, 0 skipped** (per-case outcomes in `results/security_and_integration.json`).

Unit suites on the final commit: Python 144 passed (coverage of `worker.sandbox` 95.9%, gate 90%);
TypeScript 118 passed (lines 96.9%, branches 86.9%; gates 90/85). Before the audit: 130 and 107.

### Executor latency

Hello-world per language, milliseconds. Run 1 (21:10 IST, before the `--init` change, commit
`5ca442f`):

| language | cold total | cold container | warm n | warm total p50 | p95 | p99 | warm container p50 | p95 | peak MiB |
|---|---|---|---|---|---|---|---|---|---|
| python | 1,056 | 730 | 30 | 838 | 1,944 | 2,888 | 544 | 804 | 0 |
| javascript | 1,890 | 1,596 | 30 | 830 | 1,833 | 1,920 | 556 | 1,568 | 0 |
| bash | 733 | 474 | 30 | 772 | 1,101 | 1,112 | 512 | 695 | 0 |
| ruby | 850 | 560 | 30 | 821 | 5,189 | 8,509 | 526 | 888 | 0 |
| typescript | 1,591 | 1,322 | 15 | 1,478 | 3,640 | 3,640 | 1,192 | 2,350 | 58 |
| java | 1,836 | 1,498 | 12 | 1,449 | 1,905 | 1,905 | 1,178 | 1,603 | 38 |
| go | 2,252 | 1,909 | 10 | 1,338 | 1,967 | 1,967 | 1,034 | 1,473 | 57 |
| rust | 3,281 | 2,624 | 10 | 1,288 | 12,605 | 12,605 | 1,020 | 6,775 | 37 |

Run 2 (21:50 IST, after the `--init` change, with other agents' benchmarks running on the same VM):

| language | cold total | cold container | warm n | warm total p50 | p95 | p99 | warm container p50 | p95 | peak MiB |
|---|---|---|---|---|---|---|---|---|---|
| python | 2,702 | 2,354 | 30 | 2,014 | 6,856 | 9,304 | 1,679 | 6,276 | 5 |
| javascript | 2,279 | 1,922 | 30 | 840 | 1,116 | 2,467 | 554 | 774 | 0 |
| bash | 800 | 534 | 30 | 764 | 863 | 1,025 | 498 | 615 | 0 |
| ruby | 828 | 564 | 30 | 791 | 910 | 1,018 | 513 | 619 | 0 |
| typescript | 2,017 | 1,717 | 15 | 1,429 | 1,565 | 1,565 | 1,145 | 1,270 | 59 |
| java | 1,779 | 1,499 | 12 | 1,428 | 1,571 | 1,571 | 1,152 | 1,314 | 38 |
| go | 2,472 | 2,178 | 10 | 1,389 | 1,667 | 1,667 | 1,110 | 1,324 | 56 |
| rust | 2,050 | 1,725 | 10 | 984 | 1,155 | 1,155 | 712 | 863 | 36 |

Reading it:

* Container time (`container` columns, warm p50) is about 0.5 s for Python, JavaScript, Bash and
  Ruby, and about 1.0-1.2 s for TypeScript, Java, Go and Rust, whose rows include transpile, JIT
  start-up or compilation inside the sandbox. Rust's hello-world compile is faster than expected
  because rustc runs without `cargo` and with a tiny crate graph.
* `total` is `container` plus about 0.25-0.3 s of per-job docker CLI round trips (next section).
* Run 2 shows how noisy the host is: Python's warm p50 is 2.0 s with a 6.9 s p95 in run 2 versus
  0.84 s / 1.9 s in run 1, for identical code paths. The `--init` change cannot explain that
  (JavaScript, Bash and Ruby barely moved in the same run); co-tenant load can. Treat single cells
  as +/- a factor of two.
* Cold vs warm: cold is 1-3 s for every language but the gap to warm is inconsistent between runs
  (JavaScript cold 1.9 s in run 1 and 2.3 s in run 2 against 0.84 s warm; Python cold 1.1 s then
  2.7 s). The consistent part is that the first execution in a fresh process costs roughly
  +1 s. That is expected: there is no warm sandbox to reuse, so "warm" only removes first-use
  effects (page cache for the image, seccomp-profile generation, Go build-cache copy).
* `peak MiB` is the cgroup peak of the last run when the 50 ms sampler caught the container
  (0 means it did not; short containers often finish between samples).

### Sandbox overhead versus bare `docker run`

20 interleaved iterations per variant. Run 1 (before `--init`):

| language | n | bare p50 | bare p95 | hardened p50 | hardened p95 | executor p50 | executor p95 | hardened - bare (p50) | executor - hardened (p50) |
|---|---|---|---|---|---|---|---|---|---|
| python | 20 | 1,581 | 2,183 | 1,042 | 1,706 | 1,320 | 1,950 | -539 | 278 |
| bash | 20 | 630 | 1,841 | 508 | 1,534 | 802 | 1,966 | -122 | 294 |
| javascript | 20 | 666 | 834 | 526 | 596 | 830 | 1,048 | -140 | 304 |

Run 2 (after `--init`):

| language | n | bare p50 | bare p95 | hardened p50 | hardened p95 | executor p50 | executor p95 | hardened - bare (p50) | executor - hardened (p50) |
|---|---|---|---|---|---|---|---|---|---|
| python | 20 | 620 | 733 | 498 | 604 | 757 | 947 | -122 | 259 |
| bash | 20 | 616 | 713 | 484 | 599 | 791 | 893 | -132 | 307 |
| javascript | 20 | 682 | 777 | 571 | 700 | 875 | 1,411 | -110 | 304 |

Reading it:

* The hardened flag set is **not** slower than a bare `docker run`: hardened minus bare is
  negative in every non-outlier row (about -110 to -140 ms at p50). With `--network=none` Docker
  does not create a veth pair or attach to the default bridge, which more than pays for seccomp,
  cgroup limits and the tmpfs mounts. (The bare variant uses Docker's default bridge network, so
  this is a comparison against the *default* `docker run`, not against `--network=none` alone.)
  Run 1's Python bare p50 of 1.58 s is a noisy outlier.
* The executor adds about 0.26-0.31 s at p50 on top of the hardened command in both runs. That is
  its serial docker CLI calls, measured individually:

| docker CLI call | n | p50 ms | p95 ms |
|---|---|---|---|
| docker image inspect (image verification) | 20 | 154 | 207 |
| docker inspect <missing> (container-id lookup, before the container exists) | 20 | 143 | 186 |
| docker rm -f <missing> (post-run cleanup when --rm already removed it) | 20 | 119 | 198 |
| docker version (client+server round trip) | 20 | 121 | 245 |

  `docker image inspect` before the run (about 150 ms) plus `docker rm -f` after it (about 120 ms)
  account for the executor's overhead; the sampler's container-id lookups run concurrently with the
  container and are not on the critical path. The `rm -f` is deliberate belt and braces over
  `--rm` (unit-tested, ADR-001); each CLI call costs ~120 ms here because the CLI process start-up
  and the daemon round trip dominate, which is why an Engine API client would shave most of it.

### Timeout enforcement

Before the `--init` fix (SIGTERM ignored by PID 1; commit `5ca442f`):

| program | requested s | n | measured p50 | measured max | overshoot p50 | overshoot max |
|---|---|---|---|---|---|---|
| python_spin_default_signals | 1 | 5 | 3,674 | 4,360 | 2,674 | 3,360 |
| python_spin_default_signals | 2 | 5 | 5,478 | 6,085 | 3,478 | 4,085 |
| python_spin_default_signals | 3 | 5 | 6,776 | 11,192 | 3,776 | 8,192 |
| python_spin_default_signals | 5 | 5 | 9,378 | 24,936 | 4,378 | 19,936 |
| python_spin_handles_sigterm | 1 | 5 | 2,365 | 9,277 | 1,365 | 8,277 |
| python_spin_handles_sigterm | 2 | 5 | 2,906 | 2,922 | 906 | 922 |
| python_spin_handles_sigterm | 3 | 5 | 4,059 | 4,164 | 1,059 | 1,164 |
| python_spin_handles_sigterm | 5 | 5 | 6,420 | 10,988 | 1,420 | 5,988 |
| bash_sleep | 1 | 5 | 4,040 | 4,338 | 3,040 | 3,338 |
| bash_sleep | 2 | 5 | 4,658 | 4,714 | 2,658 | 2,714 |
| bash_sleep | 3 | 5 | 5,957 | 6,772 | 2,957 | 3,772 |
| bash_sleep | 5 | 5 | 7,905 | 8,783 | 2,905 | 3,783 |

After (commit `2ba4387`):

| program | requested s | n | measured p50 | measured max | overshoot p50 | overshoot max |
|---|---|---|---|---|---|---|
| python_spin_default_signals | 1 | 5 | 2,183 | 2,500 | 1,183 | 1,500 |
| python_spin_default_signals | 2 | 5 | 2,997 | 3,025 | 997 | 1,025 |
| python_spin_default_signals | 3 | 5 | 3,720 | 3,864 | 720 | 864 |
| python_spin_default_signals | 5 | 5 | 5,916 | 6,014 | 916 | 1,014 |
| python_spin_handles_sigterm | 1 | 5 | 1,898 | 1,989 | 898 | 989 |
| python_spin_handles_sigterm | 2 | 5 | 2,545 | 3,027 | 545 | 1,027 |
| python_spin_handles_sigterm | 3 | 5 | 3,490 | 3,516 | 490 | 516 |
| python_spin_handles_sigterm | 5 | 5 | 5,538 | 5,560 | 538 | 560 |
| bash_sleep | 1 | 5 | 2,022 | 2,816 | 1,022 | 1,816 |
| bash_sleep | 2 | 5 | 2,759 | 3,244 | 759 | 1,244 |
| bash_sleep | 3 | 5 | 3,502 | 3,581 | 502 | 581 |
| bash_sleep | 5 | 5 | 5,480 | 5,494 | 480 | 494 |

Reading it:

* `measured` = pre-spawn work (image inspect, work dir; ~0.15-0.2 s) + the requested timeout
  (the timer starts at spawn, so container start-up eats into the budget rather than adding to it)
  + kill latency + teardown and `docker rm -f` (~0.3-0.4 s).
* **Before the fix**, programs without a SIGTERM handler overshot by 2.7-4.4 s at p50 (Python spin
  loop) and 2.7-3.0 s (`sleep` under bash): the 2 s grace period was always paid in full because
  PID 1 ignored the TERM. **After**, the same programs overshoot by 0.7-1.2 s and 0.5-1.0 s. The
  `python_spin_handles_sigterm` control (it exits on TERM) went from 0.9-1.4 s to 0.5-0.9 s,
  i.e. the remaining overshoot is the fixed per-job overhead, not the signal ladder.
* Enforcement itself never failed: every sample in both runs ended `TIMEOUT` with
  `timed_out=true`, and no sandbox container was left behind after the runs
  (`docker ps -a --filter label=sandbox-managed=1` was empty).
* Large maxima in the *before* run (single samples of 8-25 s, e.g. 24.9 s for T=5) occur for trivial
  programs as well (a Ruby hello-world took 7.1 s inside the container once; a Rust one 6.8 s).
  They coincide with host contention (docker daemon stalls while other agents built images), not
  with the timeout logic, and are why maxima should not be quoted as service levels. The *after*
  run happened to be calmer (max overshoot 1.8 s) but a single run cannot prove that the stalls
  are gone.

### End-to-end API latency (one request at a time)

`sync` = `POST /v1/execute` (includes the API's 100 ms poll quantisation, ADR-009); `async` =
`POST /v1/execute/async` plus `GET /v1/jobs/{id}` every 20 ms until terminal. "non-completed" counts
requests that did not end `COMPLETED` (here: `TIMEOUT` of a hello-world). The last column is the
host's 1-minute load average when that language finished (the machine has 8 logical CPUs).

Run 1, all languages (22:03-22:16 IST):

| language | n | sync p50 | sync p95 | sync p99 | async p50 | async p95 | non-completed |
|---|---|---|---|---|---|---|---|
| python | 30 | 1,034 | 1,917 | 2,289 | 1,019 | 2,425 | 0 |
| javascript | 30 | 1,971 | 11,109 | 11,858 | 1,689 | 5,739 | 1 |
| bash | 30 | 1,622 | 2,467 | 2,518 | 1,338 | 5,136 | 0 |
| ruby | 30 | 1,150 | 4,919 | 13,975 | 874 | 2,082 | 1 |
| typescript | 15 | 6,937 | 13,089 | 13,089 | 4,543 | 8,130 | 2 |
| java | 12 | 1,418 | 1,725 | 1,725 | 1,587 | 2,724 | 0 |
| go | 10 | 2,351 | 3,444 | 3,444 | 2,306 | 6,536 | 0 |
| rust | 10 | 915 | 2,037 | 2,037 | 1,097 | 6,042 | 0 |

A second pass over python/javascript/typescript was started but aborted: the host load average
reached 30 (another workload on the VM), jobs were taking 100 s, and continuing would only have
produced noise. Its partial output is not reported.

### Throughput under concurrency

Closed loop, N client threads, each repeatedly calling sync `POST /v1/execute` with a Python
hello-world for 20 s, one worker process with `WORKER_CONCURRENCY=4`. `errors` counts responses that
were not a `COMPLETED` 200 (hello-worlds that hit the 10 s Python timeout). Memory columns are the
peak `docker stats` reading of each container during that level.

Run 1:

| client threads | completed | seconds | req/s | p50 ms | p95 ms | p99 ms | errors | peak api MiB | peak worker MiB | peak redis MiB | peak minio MiB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 15 | 22 | 0.69 | 1,322 | 2,260 | 2,260 | 0 | 52 | 80 | 29 | 307 |
| 2 | 22 | 22 | 1.02 | 1,835 | 3,458 | 3,560 | 0 | 52 | 112 | 29 | 307 |
| 4 | 22 | 28 | 0.79 | 4,502 | 6,650 | 7,974 | 0 | 52 | 144 | 29 | 307 |
| 8 | 12 | 28 | 0.42 | 8,947 | 10,880 | 10,880 | 8 | 83 | 159 | 29 | 306 |
| 16 | 56 | 27 | 2.09 | 6,996 | 8,561 | 9,628 | 0 | 57 | 142 | 28 | 303 |

Run 2:

| client threads | completed | seconds | req/s | p50 ms | p95 ms | p99 ms | errors | peak api MiB | peak worker MiB | peak redis MiB | peak minio MiB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 10 | 21 | 0.48 | 1,565 | 6,280 | 6,280 | 0 | 72 | 109 | 11.00 | 271 |
| 2 | 20 | 35 | 0.57 | 1,534 | 4,950 | 5,055 | 2 | 72 | 134 | 14.10 | 273 |
| 4 | 32 | 21 | 1.55 | 2,328 | 4,490 | 4,695 | 0 | 73 | 134 | 10.50 | 277 |
| 8 | 44 | 36 | 1.21 | 3,728 | 4,512 | 5,148 | 4 | 76 | 143 | 10.80 | 276 |
| 16 | 28 | 35 | 0.79 | 6,949 | 13,548 | 13,842 | 16 | 91 | 140 | 10.90 | 276 |

### Executor-level concurrency (no queue, no API)

`bench_executor.py concurrency` (N concurrent hello-worlds through one `SandboxExecutor`, no queue or
API) exists in the harness but **was not completed**: it was queued behind other workloads for
about two hours and, when it ran, the host load average was above 20, so it was stopped. No
numbers are reported for it.

### Memory footprint

| container | MiB |
|---|---|
| code-sandbox-api-1 | 28 |
| code-sandbox-worker-1 | 72 |
| code-sandbox-redis-1 | 36 |
| code-sandbox-minio-1 | 248 |

Peak memory per stack container during the throughput runs is in the throughput table (docker
stats sampled every second). The sandboxes themselves are bounded per container by
`--memory` (128-512 MB per language; see CONFIGURATION).

## Findings from running the stack

1. **The fixes hold in the real stack.** `benchmarks/smoke_examples.py` ran 12 requests and then
   read Redis: `XLEN sandbox:jobs` = 0, `XPENDING` = 0 and the user's concurrency set had 0 members
   (acked entries deleted, slots released by the worker); `/v1/health` reported `minio: up` (the
   `host:port` endpoint fix); malformed JSON returned 400. Real captured responses are in
   [API.md](API.md#captured-examples).
2. **Footprint is small; MinIO dominates.** Idle: API 28 MiB, worker 72 MiB, Redis 36 MiB, MinIO
   248 MiB (`docker stats`). Peaks while serving load (maximum over all concurrency levels of both runs): API 91 MiB, worker 159
   MiB, Redis 29 MiB, MinIO 307 MiB. The Python worker and the API are not the memory problem; the
   sandboxes (128-512 MB each, up to `WORKER_CONCURRENCY` at once) are.
3. **Pipeline overhead is about 0.2 s.** Python sync p50 through the API was 1,034 ms in stack run 1
   versus 838 ms for the same program through the executor alone (executor run 1): API
   validation, Redis round trips, the 100 ms poll quantisation and result storage cost roughly 0.2
   s. Async with 20 ms polling was 1,019 ms. Other languages in that run are inflated by host
   load (the load-average column), so the cross-language comparison is better made with the
   executor tables.
4. **Throughput is inconclusive on this host, and under contention it degrades into timeouts.** A
   4-slot worker running 0.5 s hello-worlds should do several jobs per second on 8 cores; measured
   was 0.4-2.1 jobs/s (run 1; load average not recorded) and 0.5-1.6 (run 2; host load average
   8-15 at the 1-minute mark, last column), and no scaling with client threads. From 8 client threads upward, hello-world jobs
   began to end `TIMEOUT` (errors column): the 10 s limit is wall-clock and includes container start,
   so CPU starvation turns into timeouts, not just slower successes. Treat these tables as a
   demonstration of behaviour on an oversubscribed host, **not** as a capacity figure. An
   executor-level concurrency run was attempted and abandoned for the same reason (next section).
5. **Timeout enforcement depends on dockerd's responsiveness.** At a host load average of about 30
   (a third-party workload on the VM, not this project's), worker logs recorded jobs with a 10 s
   timeout reported `TIMEOUT` with `duration_ms` around 100,000 and one `docker run` failing
   with exit 125. The kill ladder is a sequence of docker CLI calls, so a stalled daemon stalls it.
   The 5 s bound added to the post-KILL wait caps one stage, not the whole sequence.
6. **The repo's own end-to-end suite** (`tests/e2e/user_scenarios.test.ts`, which could not even
   start before this branch's config fix) ran against the stack once: 5 passed, 4 failed, 1 skipped
   (the worker-restart test, which needs `E2E_ALLOW_RESTART=1`). The visible failures were a
   10-way parallel run whose output was not parseable (a job returned `TIMEOUT`), a WebSocket test
   that hit its 30 s client timeout and an async job that did not finish within its polling budget:
   all consistent with the overload in finding 4, but not isolated further, so they are reported as
   open, not as passing. Re-run `make test-e2e` on an idle host.
7. **Not run:** the k6 scenarios (k6 unavailable; as written the 100-VU baseline would also be
   throttled by the default quotas, e.g. anonymous callers get 10 requests/minute and 5 concurrent
   jobs), multi-worker scaling, long soak.

## Limitations

* **Shared, noisy host.** Other agents ran builds and tests on the same VM; load average was
  intermittently above the CPU count. Tail latencies in particular are contaminated.
* **One machine, one worker process, load generator on the same host** competes with the system
  under test for CPU.
* **Small samples** (10-30 per cell); p95/p99 are worst-observed values.
* **No true cold start** (no root to drop caches); no multi-worker or multi-host scaling run; no
  long soak; Python-only throughput; `k6` scenarios from `tests/e2e/load_test.js` were not run
  (k6 not installed), so the spike (500 VU) and adversarial-mix behaviour is untested here.
* **WSL2.** cgroup v2 with the systemd driver; I/O and scheduling differ from bare metal. Nothing
  in the results should be read as a statement about production hardware.
* **Docker CLI as the transport** (ADR-001) dominates fixed cost; numbers would differ with an
  Engine API client.

## Reproduce

```bash
python benchmarks/bench_security.py
python benchmarks/bench_executor.py latency && python benchmarks/bench_executor.py overhead --runs 20 \
  && python benchmarks/bench_executor.py timeout --runs 5 && python benchmarks/bench_executor.py cli-cost
# stack tier: see benchmarks/README.md
python benchmarks/make_report.py --fill benchmarks/BENCHMARKS.md.tmpl docs/BENCHMARKS.md   # regenerate this page
```
