"""Language runtime registry — the authoritative map of language → execution configuration.

Each entry pins the image, the entrypoint argv (the source path is appended by the executor,
never shell-interpolated), the fixed source filename, whether the runtime needs a writable+exec
build directory (compiled languages), the seccomp profile, and the resource envelope.

Image references and optional pinned digests are read from the environment so the same code runs
against locally-built images in dev and digest-pinned, signed images in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import cast

from worker.sandbox.errors import UnknownLanguageError
from worker.sandbox.resource_limits import ResourceConfig, get_resource_config
from worker.sandbox.seccomp import profile_path


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything the executor needs to run one language safely."""

    id: str
    name: str
    version: str
    image: str
    image_digest: str  # "" means "not pinned" (dev mode → presence check only)
    entrypoint: list[str]
    source_filename: str
    needs_exec_build: bool
    limits: ResourceConfig = field(repr=False)

    def argv_for(self, source_path: str) -> list[str]:
        """Full in-container argv: the entrypoint with the mounted source path appended."""
        return [*self.entrypoint, source_path]

    def seccomp_profile_path(self) -> str:
        """Path to this runtime's seccomp JSON profile (generated + cached on demand)."""
        return profile_path(self.id, self.limits.network_enabled)


def _image_for(language: str) -> tuple[str, str]:
    """Resolve (image_ref, digest) for a language from the environment.

    IMAGE_PREFIX + 'sandbox-runtime-<lang>' + ':' + IMAGE_TAG, with an optional pinned digest via
    SANDBOX_DIGEST_<LANG> (e.g. SANDBOX_DIGEST_PYTHON=sha256:abc...).
    """
    prefix = os.environ.get("SANDBOX_IMAGE_PREFIX", "")
    tag = os.environ.get("SANDBOX_IMAGE_TAG", "latest")
    image = f"{prefix}sandbox-runtime-{language}:{tag}"
    digest = os.environ.get(f"SANDBOX_DIGEST_{language.upper()}", "")
    return image, digest


def _build() -> dict[str, RuntimeConfig]:
    """Construct the immutable registry of all supported runtimes."""
    specs: list[dict[str, object]] = [
        {
            "id": "python",
            "name": "Python",
            "version": "3.12",
            "entrypoint": ["python3", "-I", "-B"],
            "source_filename": "main.py",
            "needs_exec_build": False,
        },
        {
            "id": "javascript",
            "name": "JavaScript (Node.js)",
            "version": "20",
            "entrypoint": ["node"],
            "source_filename": "main.js",
            "needs_exec_build": False,
        },
        {
            "id": "typescript",
            "name": "TypeScript",
            "version": "5.7",
            "entrypoint": ["/usr/local/bin/run-code"],
            "source_filename": "main.ts",
            "needs_exec_build": False,
        },
        {
            "id": "java",
            "name": "Java",
            "version": "21",
            # Single-file source-code launch (JEP 330): `java Main.java` compiles in-memory.
            "entrypoint": ["java", "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "-Xss8m"],
            "source_filename": "Main.java",
            "needs_exec_build": False,
        },
        {
            "id": "go",
            "name": "Go",
            "version": "1.22",
            "entrypoint": ["/usr/local/bin/run-code"],
            "source_filename": "main.go",
            "needs_exec_build": True,
        },
        {
            "id": "ruby",
            "name": "Ruby",
            "version": "3.3",
            "entrypoint": ["ruby", "--disable-gems"],
            "source_filename": "main.rb",
            "needs_exec_build": False,
        },
        {
            "id": "rust",
            "name": "Rust",
            "version": "1.83",
            "entrypoint": ["/usr/local/bin/run-code"],
            "source_filename": "main.rs",
            "needs_exec_build": True,
        },
        {
            "id": "bash",
            "name": "Bash",
            "version": "5.2",
            "entrypoint": ["bash"],
            "source_filename": "main.sh",
            "needs_exec_build": False,
        },
    ]

    registry: dict[str, RuntimeConfig] = {}
    for spec in specs:
        language = str(spec["id"])
        image, digest = _image_for(language)
        registry[language] = RuntimeConfig(
            id=language,
            name=str(spec["name"]),
            version=str(spec["version"]),
            image=image,
            image_digest=digest,
            entrypoint=list(cast("list[str]", spec["entrypoint"])),
            source_filename=str(spec["source_filename"]),
            needs_exec_build=bool(spec["needs_exec_build"]),
            limits=get_resource_config(language),
        )
    return registry


_REGISTRY: dict[str, RuntimeConfig] = _build()


def get_runtime(language: str) -> RuntimeConfig:
    """Return the :class:`RuntimeConfig` for a language.

    Raises:
        UnknownLanguageError: if the language is not registered.
    """
    try:
        return _REGISTRY[language]
    except KeyError as exc:
        raise UnknownLanguageError(language) from exc


def list_runtimes() -> list[RuntimeConfig]:
    """All registered runtimes, sorted by id."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def reload_registry() -> None:
    """Rebuild the registry (e.g. after environment changes in tests)."""
    global _REGISTRY
    _REGISTRY = _build()
