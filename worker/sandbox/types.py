"""Core data types, mirroring the TypeScript contract in api/src/types/index.ts.

These dataclasses are the worker-side representation of the shared protocol. ``from_dict`` /
``to_dict`` handle (de)serialisation to the exact JSON shapes the API server reads and writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A job status is one of the constants in constants.py; kept as a plain ``str`` for JSON parity.
JobStatus = str


@dataclass(frozen=True)
class InputFile:
    """An additional read-only file mounted alongside the user code."""

    name: str
    content: str


@dataclass
class ExecutionRequest:
    """A submission to execute. Mirrors the API ``ExecutionRequest``."""

    language: str
    code: str
    stdin: str = ""
    timeout_seconds: int = 10
    env_vars: dict[str, str] = field(default_factory=dict)
    files: list[InputFile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionRequest:
        """Build a request from the JSON payload enqueued by the API."""
        raw_files = data.get("files") or []
        files = [InputFile(name=str(f["name"]), content=str(f["content"])) for f in raw_files]
        return cls(
            language=str(data["language"]),
            code=str(data["code"]),
            stdin=str(data.get("stdin") or ""),
            timeout_seconds=int(data.get("timeout_seconds") or 10),
            env_vars={str(k): str(v) for k, v in (data.get("env_vars") or {}).items()},
            files=files,
        )


@dataclass
class OutputFile:
    """A file produced by the execution and stored in object storage."""

    name: str
    size_bytes: int
    url: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the API ``OutputFile`` shape."""
        return {"name": self.name, "size_bytes": self.size_bytes, "url": self.url}


@dataclass
class ExecutionResult:
    """The result of an execution. Mirrors the API ``ExecutionResult``."""

    job_id: str
    status: JobStatus
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    wall_time_ms: int = 0
    cpu_time_ms: int = 0
    memory_bytes: int = 0
    oom_killed: bool = False
    timed_out: bool = False
    truncated: bool = False
    files: list[OutputFile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the exact JSON shape the API server expects."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "wall_time_ms": self.wall_time_ms,
            "cpu_time_ms": self.cpu_time_ms,
            "memory_bytes": self.memory_bytes,
            "oom_killed": self.oom_killed,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "files": [f.to_dict() for f in self.files],
        }
