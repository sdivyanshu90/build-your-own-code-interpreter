# syntax=docker/dockerfile:1.7
# Bash 5.2 sandbox runtime on a minimal Debian base. debian-slim ships bash but NOT wget/curl/nc,
# so network shell-outs simply do not exist (verified by tests/security). The worker runs
# `bash /sandbox/main.sh` directly. This runtime pairs with the strictest seccomp profile and the
# lowest PID limit. CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM debian:bookworm-slim

# Harden: aggressively remove every privilege-escalation and network/transfer binary. The bash
# runtime should be able to do arithmetic and string processing, nothing more.
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp tftp ssh scp sftp rsync \
               chmod chown apt apt-get dpkg; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done

# `nobody` (65534) exists on Debian.
USER 65534:65534
WORKDIR /tmp
CMD ["bash", "--version"]
