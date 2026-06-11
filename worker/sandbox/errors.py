"""Typed error hierarchy for the sandbox.

Every failure mode is a distinct, catchable type carrying structured context. Raw exception
messages are never propagated to clients — the worker maps these to a FAILED result with a safe,
generic ``stderr`` while logging the full detail.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Base class for all sandbox failures.

    Args:
        message: Human-readable, operator-facing detail (never sent verbatim to clients).
        code: Stable machine-readable code for metrics/branching.
    """

    def __init__(self, message: str, code: str = "sandbox_error") -> None:
        super().__init__(message)
        self.code = code


class UnknownLanguageError(SandboxError):
    """Raised when a request names a language with no registered runtime."""

    def __init__(self, language: str) -> None:
        super().__init__(f"unknown language: {language!r}", code="unknown_language")
        self.language = language


class ImageNotFoundError(SandboxError):
    """Raised when the runtime image is not present and cannot be pulled."""

    def __init__(self, image: str) -> None:
        super().__init__(f"runtime image not found: {image}", code="image_not_found")
        self.image = image


class ImageIntegrityError(SandboxError):
    """Raised when an image's digest does not match the pinned value."""

    def __init__(self, image: str, expected: str, actual: str) -> None:
        super().__init__(
            f"image digest mismatch for {image}: expected {expected}, got {actual}",
            code="image_integrity",
        )
        self.image = image
        self.expected = expected
        self.actual = actual


class DockerUnavailableError(SandboxError):
    """Raised when the Docker daemon cannot be reached (drives the circuit breaker)."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"docker daemon unavailable: {detail}", code="docker_unavailable")
