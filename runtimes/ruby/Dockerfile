# syntax=docker/dockerfile:1.7
# Ruby 3.3 sandbox runtime. The worker runs `ruby --disable-gems /sandbox/main.rb` directly.
# CVE scanning is enforced by `make security-scan` (Trivy) / security.yml.
FROM ruby:3.3.6-slim-bookworm

# Harden: remove privilege-escalation, network client, and package-management binaries.
RUN set -eux; \
    for bin in su sudo passwd chsh chfn newgrp gpasswd chage mount umount \
               wget curl nc ncat netcat telnet ftp ssh scp chmod chown gem bundle; do \
      rm -f "/bin/$bin" "/usr/bin/$bin" "/sbin/$bin" "/usr/sbin/$bin" \
            "/usr/local/bin/$bin" 2>/dev/null || true; \
    done

USER 65534:65534
WORKDIR /tmp
CMD ["ruby", "--version"]
