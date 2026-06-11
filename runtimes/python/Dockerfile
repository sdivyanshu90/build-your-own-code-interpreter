# syntax=docker/dockerfile:1.7
# Python 3.12 sandbox runtime.
#
# No ENTRYPOINT is set: the worker passes the interpreter argv (e.g. `python3 -I -B
# /sandbox/main.py`) as the container command directly. The image is minimal, runs as the
# unprivileged `nobody` user, and has dangerous setuid/network binaries removed.
#
# CVE posture is enforced out-of-band by `make security-scan` (Trivy) and the nightly
# security.yml workflow, which fail the build on any CRITICAL/HIGH vulnerability.
FROM python:3.12.8-slim-bookworm

# Harden: remove privilege-escalation and network client binaries the sandbox never needs.
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp ssh scp chmod chown; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done; \
    # Ensure a dedicated low-privilege identity exists (nobody/nogroup are 65534 on Debian).
    getent group sandbox >/dev/null 2>&1 || groupadd -g 65533 sandbox || true

# Drop to the unprivileged user as the final image state.
USER 65534:65534
WORKDIR /tmp
CMD ["python3", "--version"]
