# syntax=docker/dockerfile:1.7
# Go 1.22 sandbox runtime. `go run` compiles to a temp dir and executes the binary, so this
# runtime needs a writable+executable build dir — the worker mounts a `/build` tmpfs (exec) for
# it while keeping /tmp noexec. The worker runs `/usr/local/bin/run-code /sandbox/main.go`.
#
# Go 1.20+ recompiles the standard library on demand into GOCACHE; on the ephemeral /build that
# cache starts empty, so a cold `go run` would recompile stdlib (~14s) and blow the timeout. We
# therefore WARM a build cache for the common stdlib packages at image-build time and copy it into
# /build at runtime, making `go run` ~1s.
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM golang:1.22.10-bookworm

ENV GOFLAGS=-mod=mod CGO_ENABLED=0

# Warm the build cache + write the runner (executable while chmod still exists) + harden.
RUN set -eux; \
    mkdir -p /opt/gocache; \
    printf '%s\n' \
      'package main' \
      'import ("bufio";"bytes";"errors";"fmt";"io";"math";"os";"sort";"strconv";"strings";"sync";"time";"unicode")' \
      'func main(){ _=bufio.NewReader(os.Stdin);_=bytes.NewBuffer(nil);_=errors.New("");_=fmt.Sprint(1);' \
      ' _=io.EOF;_=math.Pi;_=sort.Ints;_=strconv.Itoa(1);_=strings.ToUpper("");_=sync.Mutex{};' \
      ' _=time.Now();_=unicode.IsLetter(0);_=os.Args }' \
      > /tmp/warm.go; \
    GOCACHE=/opt/gocache GOPATH=/opt/go HOME=/opt go build -o /tmp/warm /tmp/warm.go; \
    rm -f /tmp/warm /tmp/warm.go; \
    chmod -R a+rX /opt/gocache; \
    printf '%s\n' \
      '#!/bin/sh' \
      '# Seed the per-run GOCACHE from the warm baked cache so `go run` is fast.' \
      'cp -a /opt/gocache /build/.cache 2>/dev/null || mkdir -p /build/.cache' \
      'export GOCACHE=/build/.cache GOTMPDIR=/build GOPATH=/build/go HOME=/build' \
      'export GOFLAGS=-mod=mod CGO_ENABLED=0' \
      'exec go run "$1"' \
      > /usr/local/bin/run-code; \
    chmod 0555 /usr/local/bin/run-code; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp ssh scp chmod chown git; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done

USER 65534:65534
WORKDIR /tmp
CMD ["go", "version"]
