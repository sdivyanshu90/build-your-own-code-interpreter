### ENV

11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz, 8 logical CPUs, 5928 MB RAM, kernel 6.18.40.1-microsoft-standard-WSL2, Docker 29.4.3 (cgroup 2 systemd), Python 3.12.13.

### LAT

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

### LAT_RUN1

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

### OVH

| language | n | bare p50 | bare p95 | hardened p50 | hardened p95 | executor p50 | executor p95 | hardened - bare (p50) | executor - hardened (p50) |
|---|---|---|---|---|---|---|---|---|---|
| python | 20 | 620 | 733 | 498 | 604 | 757 | 947 | -122 | 259 |
| bash | 20 | 616 | 713 | 484 | 599 | 791 | 893 | -132 | 307 |
| javascript | 20 | 682 | 777 | 571 | 700 | 875 | 1,411 | -110 | 304 |

### OVH_RUN1

| language | n | bare p50 | bare p95 | hardened p50 | hardened p95 | executor p50 | executor p95 | hardened - bare (p50) | executor - hardened (p50) |
|---|---|---|---|---|---|---|---|---|---|
| python | 20 | 1,581 | 2,183 | 1,042 | 1,706 | 1,320 | 1,950 | -539 | 278 |
| bash | 20 | 630 | 1,841 | 508 | 1,534 | 802 | 1,966 | -122 | 294 |
| javascript | 20 | 666 | 834 | 526 | 596 | 830 | 1,048 | -140 | 304 |

### CLI

| docker CLI call | n | p50 ms | p95 ms |
|---|---|---|---|
| docker image inspect (image verification) | 20 | 154 | 207 |
| docker inspect <missing> (container-id lookup, before the container exists) | 20 | 143 | 186 |
| docker rm -f <missing> (post-run cleanup when --rm already removed it) | 20 | 119 | 198 |
| docker version (client+server round trip) | 20 | 121 | 245 |

### TIMEOUT_BEFORE

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

### TIMEOUT_AFTER

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

### API_RUN1

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

### THR_RUN1

| client threads | completed | seconds | req/s | p50 ms | p95 ms | p99 ms | errors | peak api MiB | peak worker MiB | peak redis MiB | peak minio MiB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 15 | 22 | 0.69 | 1,322 | 2,260 | 2,260 | 0 | 52 | 80 | 29 | 307 |
| 2 | 22 | 22 | 1.02 | 1,835 | 3,458 | 3,560 | 0 | 52 | 112 | 29 | 307 |
| 4 | 22 | 28 | 0.79 | 4,502 | 6,650 | 7,974 | 0 | 52 | 144 | 29 | 307 |
| 8 | 12 | 28 | 0.42 | 8,947 | 10,880 | 10,880 | 8 | 83 | 159 | 29 | 306 |
| 16 | 56 | 27 | 2.09 | 6,996 | 8,561 | 9,628 | 0 | 57 | 142 | 28 | 303 |

### THR_RUN2

| client threads | completed | seconds | req/s | p50 ms | p95 ms | p99 ms | errors | peak api MiB | peak worker MiB | peak redis MiB | peak minio MiB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 10 | 21 | 0.48 | 1,565 | 6,280 | 6,280 | 0 | 72 | 109 | 11.00 | 271 |
| 2 | 20 | 35 | 0.57 | 1,534 | 4,950 | 5,055 | 2 | 72 | 134 | 14.10 | 273 |
| 4 | 32 | 21 | 1.55 | 2,328 | 4,490 | 4,695 | 0 | 73 | 134 | 10.50 | 277 |
| 8 | 44 | 36 | 1.21 | 3,728 | 4,512 | 5,148 | 4 | 76 | 143 | 10.80 | 276 |
| 16 | 28 | 35 | 0.79 | 6,949 | 13,548 | 13,842 | 16 | 91 | 140 | 10.90 | 276 |

### IDLE

| container | MiB |
|---|---|
| code-sandbox-api-1 | 28 |
| code-sandbox-worker-1 | 72 |
| code-sandbox-redis-1 | 36 |
| code-sandbox-minio-1 | 248 |

### SEC

`bench_security.py` (`tests/security` + `tests/integration`, real containers): **59 passed, 0 failed, 0 errors, 0 skipped** (per-case outcomes in `results/security_and_integration.json`).
