Environment: 11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz, 8 CPUs, 5928 MB RAM, kernel 6.18.40.1-microsoft-standard-WSL2, Docker 29.4.3 (2 systemd), commit 5ca442f, 2026-10-04T15:44:24+00:00


### Executor latency (ms), hello world

| language | cold total | warm n | warm p50 | warm p95 | warm p99 | container p50 | container p95 | peak MiB |
|---|---|---|---|---|---|---|---|---|
| python | 1,056 | 30 | 838 | 1,944 | 2,888 | 544 | 804 | 0 |
| javascript | 1,890 | 30 | 830 | 1,833 | 1,920 | 556 | 1,568 | 0 |
| bash | 733 | 30 | 772 | 1,101 | 1,112 | 512 | 695 | 0 |
| ruby | 850 | 30 | 821 | 5,189 | 8,509 | 526 | 888 | 0 |
| typescript | 1,591 | 15 | 1,478 | 3,640 | 3,640 | 1,192 | 2,350 | 58 |
| java | 1,836 | 12 | 1,449 | 1,905 | 1,905 | 1,178 | 1,603 | 38 |
| go | 2,252 | 10 | 1,338 | 1,967 | 1,967 | 1,034 | 1,473 | 57 |
| rust | 3,281 | 10 | 1,288 | 12,605 | 12,605 | 1,020 | 6,775 | 37 |

### Overhead versus bare `docker run` (ms)

| language | n | bare p50 | bare p95 | hardened p50 | hardened p95 | executor p50 | executor p95 | hardened - bare (p50) | executor - hardened (p50) |
|---|---|---|---|---|---|---|---|---|---|
| python | 20 | 1,581 | 2,183 | 1,042 | 1,706 | 1,320 | 1,950 | -539 | 278 |
| bash | 20 | 630 | 1,841 | 508 | 1,534 | 802 | 1,966 | -122 | 294 |
| javascript | 20 | 666 | 834 | 526 | 596 | 830 | 1,048 | -140 | 304 |

### Timeout enforcement (ms)

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

### Security + integration suites

passed 49, failed 0, errors 0, skipped 0
