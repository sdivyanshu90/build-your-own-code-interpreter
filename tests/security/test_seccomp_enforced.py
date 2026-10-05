"""Prove that the seccomp profile (not just dropped capabilities or deleted binaries) is enforced.

The escape payloads in ``test_escape_attempts.py`` mostly invoke userspace tools (``mount``,
``modprobe``, ``unshare``) that the runtime images simply do not contain, so a "blocked" result
there does not show the kernel filter works. These tests call the syscalls directly and pick ones
whose result *differs* with and without the filter on a stock kernel:

* ``unshare(CLONE_NEWUSER)`` succeeds for an unprivileged process unless seccomp denies it;
* ``io_uring_setup`` / ``userfaultfd`` / ``perf_event_open`` / ``keyctl`` reach the kernel (and fail
  with something other than EPERM, or succeed) unless seccomp denies them.

Each must fail with ``EPERM`` (errno 1), which is what the profiles return. x86_64 syscall numbers.
"""

from __future__ import annotations

import pytest

from worker.sandbox.types import ExecutionRequest

pytestmark = pytest.mark.integration

EPERM = 1

# name -> (x86_64 syscall number, args)
SYSCALLS = {
    "unshare_newuser": (272, "0x10000000, 0, 0"),
    "io_uring_setup": (425, "0, 0, 0"),
    "userfaultfd": (323, "0, 0, 0"),
    "perf_event_open": (298, "0, 0, 0"),
    "keyctl": (250, "0, 0, 0"),
    "ptrace_traceme": (101, "0, 0, 0"),
}

PYTHON_TEMPLATE = (
    "import ctypes, platform\n"
    "if platform.machine() != 'x86_64':\n"
    "    print('SKIP-ARCH'); raise SystemExit\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "r = libc.syscall({nr}, {args})\n"
    "print('errno', ctypes.get_errno() if r == -1 else 0)\n"
)

GO_TEMPLATE = (
    "package main\n"
    'import ("fmt"; "runtime"; "syscall")\n'
    "func main() {{\n"
    '  if runtime.GOARCH != "amd64" {{ fmt.Println("SKIP-ARCH"); return }}\n'
    "  _, _, e := syscall.Syscall({nr}, {args})\n"
    '  fmt.Println("errno", int(e))\n'
    "}}\n"
)


def _go_args(args: str) -> str:
    return args  # Syscall takes three uintptr args; the literals above already have three


async def _errno(real_executor, language: str, code: str, name: str) -> str:
    request = ExecutionRequest(language=language, code=code, timeout_seconds=20)
    result = await real_executor.execute(request, f"seccomp-{language}-{name}")
    assert result.status == "COMPLETED", (result.status, result.stderr[-300:])
    return result.stdout.strip()


@pytest.mark.parametrize("name", list(SYSCALLS))
async def test_allowlist_profile_denies_syscall_with_eperm(name, real_executor, require_language):
    """python uses the default-deny allow-list profile."""
    require_language("python")
    nr, args = SYSCALLS[name]
    out = await _errno(real_executor, "python", PYTHON_TEMPLATE.format(nr=nr, args=args), name)
    if "SKIP-ARCH" in out:
        pytest.skip("syscall numbers in this test are x86_64")
    assert out == f"errno {EPERM}", f"{name}: expected EPERM from seccomp, got {out!r}"


@pytest.mark.parametrize("name", ["unshare_newuser", "io_uring_setup", "userfaultfd"])
async def test_blocklist_profile_denies_syscall_with_eperm(name, real_executor, require_language):
    """go uses the default-allow block-list profile; the dangerous syscalls must still be denied."""
    require_language("go")
    nr, args = SYSCALLS[name]
    out = await _errno(real_executor, "go", GO_TEMPLATE.format(nr=nr, args=_go_args(args)), name)
    if "SKIP-ARCH" in out:
        pytest.skip("syscall numbers in this test are x86_64")
    assert out == f"errno {EPERM}", f"{name}: expected EPERM from seccomp, got {out!r}"
