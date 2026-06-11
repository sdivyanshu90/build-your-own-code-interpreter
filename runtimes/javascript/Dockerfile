# syntax=docker/dockerfile:1.7
# JavaScript (Node.js 20) sandbox runtime on Alpine.
# No ENTRYPOINT — the worker runs `node <flags> /sandbox/main.js` directly as `nobody`.
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM node:20.18.1-alpine3.20

# Harden: BusyBox bundles wget/nc as applets; remove them and other risky binaries so the
# sandbox cannot shell out to the network or escalate privileges.
RUN set -eux; \
    for bin in su sudo passwd wget curl nc ncat netcat telnet ftp ssh scp \
               chmod chown mount umount; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done

# `nobody` (65534) already exists on Alpine.
USER 65534:65534
WORKDIR /tmp
CMD ["node", "--version"]
