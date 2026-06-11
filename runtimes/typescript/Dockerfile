# syntax=docker/dockerfile:1.7
# TypeScript 5.7 sandbox runtime: Node.js + tsx (esbuild-based) for in-memory transpilation, so
# no writable/executable build directory is required. The worker runs
# `/usr/local/bin/run-code /sandbox/main.ts`.
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM node:20.18.1-alpine3.20

# Install tsx + the TypeScript compiler globally, pinned, then strip the npm/network toolchain
# and risky binaries so nothing can install or fetch at runtime.
RUN set -eux; \
    npm install -g --no-audit --no-fund tsx@4.19.2 typescript@5.7.2; \
    npm cache clean --force; \
    for bin in npm npx su sudo passwd wget curl nc ncat netcat telnet ftp ssh scp \
               chmod chown mount umount; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" \
            "/usr/local/bin/$bin" 2>/dev/null || true; \
    done

# Runner: transpile-and-run the TypeScript entrypoint in one step.
RUN printf '#!/bin/sh\nexec tsx "$1"\n' > /usr/local/bin/run-code \
 && chmod 0555 /usr/local/bin/run-code

# Cache dir for tsx must be writable; point it at the writable (noexec) /tmp.
ENV XDG_CACHE_HOME=/tmp/.cache TSX_DISABLE_CACHE=1

USER 65534:65534
WORKDIR /tmp
CMD ["tsx", "--version"]
