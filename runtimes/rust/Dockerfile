# syntax=docker/dockerfile:1.7
# Rust 1.83 sandbox runtime. `rustc` compiles to a binary that must be executed, so this runtime
# uses the writable+executable `/build` tmpfs (worker-provided) while /tmp stays noexec. The
# worker runs `/usr/local/bin/run-code /sandbox/main.rs`.
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM rust:1.83.0-slim-bookworm

# Harden: remove privilege-escalation, network client, and package-management binaries (cargo's
# registry fetching is irrelevant — there is no network, only the std crate is available).
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp ssh scp chmod chown cargo git; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done

# Runner: compile (errors go to stderr, yielding a non-zero exit) then exec the produced binary.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'export HOME=/build' \
    'rustc -O --edition 2021 -o /build/prog "$1"' \
    'exec /build/prog' \
    > /usr/local/bin/run-code \
 && chmod 0555 /usr/local/bin/run-code

USER 65534:65534
WORKDIR /tmp
CMD ["rustc", "--version"]
