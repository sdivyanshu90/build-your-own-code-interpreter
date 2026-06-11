# syntax=docker/dockerfile:1.7
# Go 1.22 sandbox runtime. `go run` compiles to a temp dir and executes the binary, so this
# runtime needs a writable+executable build dir — the worker mounts a `/build` tmpfs (exec) for
# it while keeping /tmp noexec. The worker runs `/usr/local/bin/run-code /sandbox/main.go`.
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM golang:1.22.10-bookworm

# Harden: remove privilege-escalation and network client binaries (git/wget/curl included — no
# module fetching at runtime; only the standard library is available with --network none).
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp ssh scp chmod chown git; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done

# Runner: confine all of Go's scratch state to the writable /build tmpfs and run the program.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'export GOCACHE=/build/.cache GOTMPDIR=/build GOPATH=/build/go HOME=/build' \
    'export GOFLAGS=-mod=mod CGO_ENABLED=0' \
    'exec go run "$1"' \
    > /usr/local/bin/run-code \
 && chmod 0555 /usr/local/bin/run-code

USER 65534:65534
WORKDIR /tmp
CMD ["go", "version"]
