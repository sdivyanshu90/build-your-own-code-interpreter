"""Seccomp profile builder.

Generates Docker-compatible seccomp JSON profiles with a **default-deny** posture
(``defaultAction: SCMP_ACT_ERRNO``): any syscall not explicitly allow-listed fails with
``EPERM`` instead of executing. Each runtime gets the minimal set of syscalls it needs.

Three layers of safety:

1. A curated ``COMMON_SYSCALLS`` allow-list shared by the interpreted runtimes, plus per-language
   extras. Toolchain-heavy runtimes (typescript/go/rust) instead use a default-ALLOW profile that
   denies the dangerous syscalls (see ``BLOCKLIST_LANGUAGES`` / ``_build_blocklist_profile``).
2. ``DANGEROUS_SYSCALLS`` — escape/privilege primitives that are *subtracted* from every profile
   defensively, so they can never be allow-listed even by mistake.
3. ``clone`` is allowed only with a masked-argument rule that forbids the namespace-creation
   flags (the standard Docker mask), and ``clone3`` returns ``ENOSYS`` to force the glibc fallback
   to the restricted ``clone``.

Run ``python -m worker.sandbox.seccomp <language>`` to emit a profile to stdout (used to
regenerate the static ``runtimes/<lang>/seccomp-profile.json`` artifacts).
"""

from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
from typing import Any, Final

# Bitmask of CLONE_NEW* namespace flags (matches the upstream Docker default profile).
# A clone() whose flags AND this mask is non-zero is denied (it would create a namespace).
_CLONE_NAMESPACE_MASK: Final[int] = 0x7E020000  # 2114060288

# Individual CLONE_NEW* flags, used to deny namespace-creating clone() under the blocklist model
# (where the default is ALLOW, so each flag needs its own deny rule: (arg0 & FLAG) == FLAG).
_CLONE_NAMESPACE_FLAGS: Final[tuple[int, ...]] = (
    0x00020000,  # CLONE_NEWNS
    0x02000000,  # CLONE_NEWCGROUP
    0x04000000,  # CLONE_NEWUTS
    0x08000000,  # CLONE_NEWIPC
    0x10000000,  # CLONE_NEWUSER
    0x20000000,  # CLONE_NEWPID
    0x40000000,  # CLONE_NEWNET
)

# ENOSYS, returned for clone3 so libc falls back to the masked clone().
_ENOSYS: Final[int] = 38

# Languages whose toolchains (a transpiler/loader or a compiler that spawns a linker and many
# helper processes/threads) need a syscall surface too broad to safely enumerate as an allow-list.
# These use a default-ALLOW profile that still denies every DANGEROUS_SYSCALLS entry and blocks
# namespace-creating clone()/clone3() — the same model as Docker's vetted default profile, paired
# with this project's stricter cap-drop, no-network, read-only, and no-new-privileges layers.
BLOCKLIST_LANGUAGES: Final[frozenset[str]] = frozenset({"typescript", "go", "rust"})

ARCHITECTURES: Final[list[str]] = [
    "SCMP_ARCH_X86_64",
    "SCMP_ARCH_X86",
    "SCMP_ARCH_X32",
    "SCMP_ARCH_AARCH64",
    "SCMP_ARCH_ARM",
]

# Syscalls that must NEVER appear in any profile: container-escape, privilege-escalation,
# kernel-module, mount, keyring, ptrace, and noexec-bypass primitives.
DANGEROUS_SYSCALLS: Final[frozenset[str]] = frozenset(
    {
        # tracing / memory peeking
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
        "kcmp",
        # kernel modules
        "init_module",
        "finit_module",
        "delete_module",
        "create_module",
        "get_kernel_syms",
        "query_module",
        # mount / fs namespace manipulation (classic + new mount API)
        "mount",
        "umount",
        "umount2",
        "mount_setattr",
        "move_mount",
        "open_tree",
        "fsopen",
        "fsconfig",
        "fspick",
        "pivot_root",
        "chroot",
        # namespace manipulation
        "unshare",
        "setns",
        # bpf / keyrings / perf
        "bpf",
        "keyctl",
        "add_key",
        "request_key",
        "perf_event_open",
        # fault handling / async io escape surface
        "userfaultfd",
        "io_uring_setup",
        "io_uring_enter",
        "io_uring_register",
        "io_setup",
        "io_destroy",
        "io_submit",
        "io_cancel",
        "io_getevents",
        # power / time / accounting / quota
        "kexec_load",
        "kexec_file_load",
        "reboot",
        "swapon",
        "swapoff",
        "settimeofday",
        "stime",
        "clock_settime",
        "clock_settime64",
        "clock_adjtime",
        "clock_adjtime64",
        "adjtimex",
        "acct",
        "quotactl",
        "nfsservctl",
        # privileged hardware / ldt / personality
        "ioperm",
        "iopl",
        "modify_ldt",
        "vm86",
        "vm86old",
        "personality",
        # handle-based fs escape
        "open_by_handle_at",
        "name_to_handle_at",
        "fanotify_init",
        "fanotify_mark",
        # misc legacy / info-leak
        "lookup_dcookie",
        "sysfs",
        "_sysctl",
        "vhangup",
        # noexec bypass via anonymous executable memory
        "memfd_create",
        "memfd_secret",
        # device node creation
        "mknod",
        "mknodat",
        # uid/gid/capability changes (we already run as nobody with no caps)
        "setuid",
        "setgid",
        "setreuid",
        "setregid",
        "setresuid",
        "setresgid",
        "setfsuid",
        "setfsgid",
        "setgroups",
        "capset",
    }
)

# A broad, safe allow-list covering file/IO, memory, scheduling, signals, time, and process
# basics that real interpreted and compiled runtimes need. Deliberately excludes clone/clone3
# (handled specially) and everything in DANGEROUS_SYSCALLS.
COMMON_SYSCALLS: Final[frozenset[str]] = frozenset(
    {
        # file IO
        "read",
        "write",
        "readv",
        "writev",
        "pread64",
        "pwrite64",
        "preadv",
        "pwritev",
        "preadv2",
        "pwritev2",
        "open",
        "openat",
        "openat2",
        "close",
        "close_range",
        "creat",
        "lseek",
        "_llseek",
        "dup",
        "dup2",
        "dup3",
        "pipe",
        "pipe2",
        "fcntl",
        "fcntl64",
        "flock",
        "fsync",
        "fdatasync",
        "sync",
        "syncfs",
        "sync_file_range",
        "fadvise64",
        "fadvise64_64",
        "readahead",
        "sendfile",
        "sendfile64",
        "copy_file_range",
        "splice",
        "tee",
        "vmsplice",
        "ioctl",
        # file metadata / dirs
        "stat",
        "fstat",
        "lstat",
        "newfstatat",
        "statx",
        "fstatat64",
        "stat64",
        "lstat64",
        "fstat64",
        "statfs",
        "fstatfs",
        "statfs64",
        "fstatfs64",
        "getdents",
        "getdents64",
        "getcwd",
        "chdir",
        "fchdir",
        "rename",
        "renameat",
        "renameat2",
        "mkdir",
        "mkdirat",
        "rmdir",
        "link",
        "linkat",
        "unlink",
        "unlinkat",
        "symlink",
        "symlinkat",
        "readlink",
        "readlinkat",
        "chmod",
        "fchmod",
        "fchmodat",
        "chown",
        "fchown",
        "lchown",
        "fchownat",
        "umask",
        "access",
        "faccessat",
        "faccessat2",
        "truncate",
        "ftruncate",
        "truncate64",
        "ftruncate64",
        "utime",
        "utimes",
        "utimensat",
        "utimensat_time64",
        "futimesat",
        # memory
        "mmap",
        "mmap2",
        "munmap",
        "mremap",
        "mprotect",
        "msync",
        "mincore",
        "madvise",
        "mlock",
        "munlock",
        "mlock2",
        "mlockall",
        "munlockall",
        "brk",
        "get_mempolicy",
        "set_mempolicy",
        "membarrier",
        # signals
        "rt_sigaction",
        "rt_sigprocmask",
        "rt_sigreturn",
        "rt_sigpending",
        "rt_sigsuspend",
        "rt_sigtimedwait",
        "rt_sigtimedwait_time64",
        "rt_sigqueueinfo",
        "rt_tgsigqueueinfo",
        "sigaltstack",
        "signalfd",
        "signalfd4",
        "kill",
        "tgkill",
        "tkill",
        # process / identity (read-only or self-only)
        "getpid",
        "getppid",
        "gettid",
        "getuid",
        "geteuid",
        "getgid",
        "getegid",
        "getgroups",
        "getresuid",
        "getresgid",
        "getpgrp",
        "getpgid",
        "setpgid",
        "getsid",
        "setsid",
        "getpriority",
        "setpriority",
        "set_tid_address",
        "set_robust_list",
        "get_robust_list",
        "prctl",
        "arch_prctl",
        "getcpu",
        "capget",
        # process lifecycle
        "execve",
        "execveat",
        "fork",
        "vfork",
        "exit",
        "exit_group",
        "wait4",
        "waitid",
        # threads / futex / scheduling
        "futex",
        "futex_waitv",
        "futex_time64",
        "set_thread_area",
        "get_thread_area",
        "sched_yield",
        "sched_getaffinity",
        "sched_setaffinity",
        "sched_getparam",
        "sched_setparam",
        "sched_getscheduler",
        "sched_setscheduler",
        "sched_get_priority_max",
        "sched_get_priority_min",
        "sched_rr_get_interval",
        "sched_rr_get_interval_time64",
        "sched_getattr",
        "sched_setattr",
        "rseq",
        # time / sleep
        "nanosleep",
        "clock_nanosleep",
        "clock_nanosleep_time64",
        "clock_gettime",
        "clock_gettime64",
        "clock_getres",
        "clock_getres_time64",
        "gettimeofday",
        "time",
        "times",
        "getrandom",
        # polling / events
        "poll",
        "ppoll",
        "ppoll_time64",
        "select",
        "_newselect",
        "pselect6",
        "pselect6_time64",
        "epoll_create",
        "epoll_create1",
        "epoll_ctl",
        "epoll_wait",
        "epoll_pwait",
        "epoll_pwait2",
        "eventfd",
        "eventfd2",
        "timerfd_create",
        "timerfd_settime",
        "timerfd_settime64",
        "timerfd_gettime",
        "timerfd_gettime64",
        "inotify_init",
        "inotify_init1",
        "inotify_add_watch",
        "inotify_rm_watch",
        "restart_syscall",
        # limits / info
        "getrlimit",
        "setrlimit",
        "prlimit64",
        "getrusage",
        "uname",
        "sysinfo",
    }
)

# Per-language additions on top of COMMON_SYSCALLS.
LANGUAGE_EXTRA: Final[dict[str, frozenset[str]]] = {
    "python": frozenset(),
    "javascript": frozenset({"epoll_pwait", "statx"}),
    "typescript": frozenset({"epoll_pwait", "statx"}),
    "java": frozenset({"get_mempolicy", "set_mempolicy", "getcpu", "sched_setaffinity"}),
    "go": frozenset({"sched_getaffinity", "rseq"}),
    "ruby": frozenset(),
    "rust": frozenset(),
}

# Network syscalls, added only when a runtime is granted egress (default: not added).
NETWORK_SYSCALLS: Final[frozenset[str]] = frozenset(
    {
        "socket",
        "connect",
        "bind",
        "listen",
        "accept",
        "accept4",
        "getsockname",
        "getpeername",
        "sendto",
        "recvfrom",
        "sendmsg",
        "recvmsg",
        "sendmmsg",
        "recvmmsg",
        "recvmmsg_time64",
        "getsockopt",
        "setsockopt",
        "shutdown",
    }
)

# socketpair is always allowed: it only creates a connected AF_UNIX/AF_LOCAL pair (no external
# reachability), which some runtimes use for internal IPC. It is never an exfiltration vector.
_ALWAYS_LOCAL: Final[frozenset[str]] = frozenset({"socketpair"})


def allowed_syscalls(language: str, network_enabled: bool = False) -> frozenset[str]:
    """Compute the final default-deny allow-list for an interpreted language.

    All interpreted runtimes (python/javascript/java/ruby/bash) share the curated
    ``COMMON_SYSCALLS`` base plus any per-language extras. The base is deliberately broad enough to
    satisfy the container runtime's init (e.g. ``capget``/``fstatfs``) and a real shell/interpreter
    startup, while still being default-deny and excluding the network and dangerous syscalls.
    """
    allowed = set(COMMON_SYSCALLS) | set(LANGUAGE_EXTRA.get(language, frozenset()))
    allowed |= _ALWAYS_LOCAL
    if network_enabled:
        allowed |= NETWORK_SYSCALLS
    # Defensive subtraction: dangerous syscalls can never survive, regardless of the lists above.
    allowed -= DANGEROUS_SYSCALLS
    return frozenset(allowed)


def _build_allowlist_profile(language: str, network_enabled: bool) -> dict[str, Any]:
    """Default-DENY profile: only the per-language allow-list runs (interpreted runtimes)."""
    allowed = allowed_syscalls(language, network_enabled)
    syscalls: list[dict[str, Any]] = [
        {"names": sorted(allowed), "action": "SCMP_ACT_ALLOW"},
        # clone allowed only when it creates no new namespace (flags & mask == 0).
        {
            "names": ["clone"],
            "action": "SCMP_ACT_ALLOW",
            "args": [
                {
                    "index": 0,
                    "value": _CLONE_NAMESPACE_MASK,
                    "valueTwo": 0,
                    "op": "SCMP_CMP_MASKED_EQ",
                }
            ],
        },
        # clone3 -> ENOSYS so glibc falls back to the masked clone above.
        {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": _ENOSYS},
    ]
    return {
        "defaultAction": "SCMP_ACT_ERRNO",
        "defaultErrnoRet": 1,
        "architectures": ARCHITECTURES,
        "syscalls": syscalls,
    }


def _build_blocklist_profile() -> dict[str, Any]:
    """Default-ALLOW profile that denies every dangerous syscall and namespace-creating clone.

    Used for toolchain-heavy runtimes (see :data:`BLOCKLIST_LANGUAGES`). It mirrors the model of
    Docker's vetted default profile: the broad set of ordinary syscalls a compiler/linker needs is
    permitted, while the escape/privilege primitives in :data:`DANGEROUS_SYSCALLS` are blocked with
    ``EPERM``, ``clone3`` is forced to fall back via ``ENOSYS``, and ``clone`` with any CLONE_NEW*
    flag is denied so no new namespace can be created.
    """
    syscalls: list[dict[str, Any]] = [
        {"names": sorted(DANGEROUS_SYSCALLS), "action": "SCMP_ACT_ERRNO", "errnoRet": 1},
        {"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": _ENOSYS},
    ]
    # One deny rule per namespace flag: deny clone when (arg0 & FLAG) == FLAG (the flag is set).
    for flag in _CLONE_NAMESPACE_FLAGS:
        syscalls.append(
            {
                "names": ["clone"],
                "action": "SCMP_ACT_ERRNO",
                "errnoRet": 1,
                "args": [{"index": 0, "value": flag, "valueTwo": flag, "op": "SCMP_CMP_MASKED_EQ"}],
            }
        )
    return {
        "defaultAction": "SCMP_ACT_ALLOW",
        "architectures": ARCHITECTURES,
        "syscalls": syscalls,
    }


def build_profile(language: str, network_enabled: bool = False) -> dict[str, Any]:
    """Build a complete Docker seccomp profile dict for a language.

    Interpreted runtimes get a tight default-deny allow-list; toolchain-heavy runtimes
    (:data:`BLOCKLIST_LANGUAGES`) get a default-allow profile that still blocks every dangerous
    syscall and namespace creation.
    """
    if language in BLOCKLIST_LANGUAGES:
        return _build_blocklist_profile()
    return _build_allowlist_profile(language, network_enabled)


@functools.lru_cache(maxsize=32)
def profile_path(language: str, network_enabled: bool = False) -> str:
    """Write the profile to a stable cache file and return its path.

    The path must be readable by the ``docker`` CLI the worker invokes (it embeds the JSON into
    the container create request). Cached so each profile is written at most once per process.
    """
    cache_dir = os.environ.get("SECCOMP_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "sandbox-seccomp"
    )
    os.makedirs(cache_dir, mode=0o755, exist_ok=True)
    suffix = "-net" if network_enabled else ""
    path = os.path.join(cache_dir, f"{language}{suffix}.json")
    profile = build_profile(language, network_enabled)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, sort_keys=True)
    return path


def _main(argv: list[str]) -> int:
    """CLI: print a language's profile JSON (used to regenerate the static artifacts)."""
    if len(argv) < 2:
        print("usage: python -m worker.sandbox.seccomp <language> [--network]", file=sys.stderr)
        return 2
    language = argv[1]
    network = "--network" in argv[2:]
    print(json.dumps(build_profile(language, network), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
