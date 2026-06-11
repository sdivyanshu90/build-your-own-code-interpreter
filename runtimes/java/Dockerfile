# syntax=docker/dockerfile:1.7
# Java 21 sandbox runtime. Uses single-file source-code launch (JEP 330): `java /sandbox/Main.java`
# compiles in-memory and runs — no writable build directory needed. The worker passes the `java`
# argv directly (no ENTRYPOINT). CVE scanning is enforced by `make security-scan` / security.yml.
FROM eclipse-temurin:21.0.5_11-jdk-noble

# Harden: remove privilege-escalation and network client binaries.
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp ssh scp chmod chown; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" 2>/dev/null || true; \
    done

# `nobody` (65534) exists on Ubuntu Noble.
USER 65534:65534
WORKDIR /tmp
CMD ["java", "--version"]
