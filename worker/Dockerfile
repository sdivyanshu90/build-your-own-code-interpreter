# syntax=docker/dockerfile:1.7
# The worker daemon image. It needs the Docker CLI to drive sibling sandbox containers via the
# host Docker socket (mounted at runtime), but it never runs untrusted code itself.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install the Docker CLI (client only — no daemon) and tini for clean signal handling.
# Versions are pinned via the Docker apt repo for reproducibility.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tini \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/worker/
# Make `worker` importable as a top-level package.
ENV PYTHONPATH=/app

# Per-job work dirs are written here; bind-mount a host path at the same location in production
# so the Docker daemon can see the code directory for the read-only sandbox mount.
ENV SANDBOX_WORKDIR=/var/sandbox-work
RUN mkdir -p /var/sandbox-work

EXPOSE 9100
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "worker.worker"]
