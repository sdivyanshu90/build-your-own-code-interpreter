"""Garbage collection for leaked sandbox containers and stale work directories.

Containers are normally removed by ``--rm`` plus the executor's explicit ``docker rm -f``. If a
worker dies between ``create`` and ``rm``, a container can leak. The reaper periodically removes
any ``sandbox-managed`` container older than a safety threshold (well beyond the max timeout) and
deletes orphaned per-job work directories.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Awaitable, Callable

from worker.config import WorkerConfig

SimpleFn = Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]]

# Containers/workdirs older than this are considered orphaned. Generously above any job timeout.
ORPHAN_AGE_SECONDS = 300


class ContainerReaper:
    """Periodically removes leaked sandbox containers and stale work directories."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        simple: SimpleFn | None = None,
        interval_seconds: int = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._simple = simple or _default_simple
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        """Run reap cycles until :meth:`stop` is called."""
        while not self._stop.is_set():
            await self.reap_once()
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Signal the reaper to exit after the current cycle."""
        self._stop.set()

    async def reap_once(self) -> int:
        """Remove orphaned containers and workdirs once; return the number of containers reaped."""
        reaped = await self._reap_containers()
        self._reap_workdirs()
        return reaped

    async def _reap_containers(self) -> int:
        """Remove sandbox containers that have exited, or that have run past the orphan threshold.

        Crucially this must NOT remove young, still-running containers — those are the active jobs
        of this or other workers. We reap a running container only when it is clearly orphaned
        (running longer than ORPHAN_AGE_SECONDS, which is far beyond any legal job timeout).
        """
        rc, out, _ = await self._simple(
            [
                self.config.docker_path,
                "ps",
                "-a",
                "--filter",
                "label=sandbox-managed=1",
                "--format",
                "{{.ID}}\t{{.Status}}",
                "--no-trunc",
            ]
        )
        if rc != 0:
            return 0
        reaped = 0
        for line in out.decode("utf-8", "replace").splitlines():
            if "\t" not in line:
                continue
            container_id, status = line.split("\t", 1)
            if should_reap(status):
                await self._simple([self.config.docker_path, "rm", "-f", container_id])
                reaped += 1
        return reaped

    def _reap_workdirs(self) -> None:
        """Delete per-job work directories older than the orphan threshold."""
        base = self.config.local_workdir
        if not os.path.isdir(base):
            return
        now = self._clock()
        for entry in os.listdir(base):
            path = os.path.join(base, entry)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age > ORPHAN_AGE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)


_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}


def _parse_up_age(status: str) -> int | None:
    """Best-effort parse of a Docker 'Up ...' status into elapsed seconds.

    Handles forms like 'Up 7 seconds', 'Up 6 minutes', 'Up About a minute', 'Up 2 hours'.
    Returns None when the age cannot be determined (in which case the container is left alone).
    """
    text = status[len("Up ") :].strip().lower()
    if text.startswith("less than"):
        return 0
    if text.startswith("about "):
        text = text[len("about ") :]
    parts = text.split()
    if not parts:
        return None
    qty_word = parts[0]
    quantity = 1 if qty_word in ("a", "an") else _safe_int(qty_word)
    if quantity is None:
        return None
    for unit, seconds in _UNIT_SECONDS.items():
        if any(p.startswith(unit) for p in parts[1:]):
            return quantity * seconds
    return None


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def should_reap(status: str) -> bool:
    """Decide whether a container in the given Docker status should be removed."""
    stripped = status.strip()
    if stripped.startswith(("Exited", "Dead", "Created", "Removal")):
        return True
    if stripped.startswith("Up"):
        age = _parse_up_age(stripped)
        return age is not None and age >= ORPHAN_AGE_SECONDS
    return False


async def _default_simple(argv: list[str]) -> tuple[int, bytes, bytes]:
    """Run a one-shot docker command, returning (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out, err
