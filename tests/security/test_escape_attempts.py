"""Security suite: attempt every known container-escape technique and verify each is blocked.

Each payload is written so that the string ``ESCAPED`` is printed *only if the dangerous
operation actually succeeds*. The assertion is therefore precise: a container-local action that
is harmless (e.g. reading the container's own /etc/passwd) is not mistaken for an escape, while a
genuine breakout (reading host secrets, opening a socket, gaining privilege) would print
``ESCAPED`` and fail the test.

These run against REAL containers and are marked ``integration``; they skip when Docker or the
runtime image is unavailable. Run with:  pytest -m integration tests/security
"""

from __future__ import annotations

import pytest

from worker.sandbox.types import ExecutionRequest

pytestmark = pytest.mark.integration

SENTINEL = "ESCAPED"

# (name, language, code). Each prints SENTINEL only on a successful breakout.
ESCAPE_ATTEMPTS = [
    # ── Filesystem escapes ────────────────────────────────────────────────────────────────
    (
        "read_etc_shadow",
        "python",
        "try:\n d=open('/etc/shadow').read()\n print('ESCAPED' if 'root:' in d else 'no')\n"
        "except Exception: print('blocked')",
    ),
    (
        "read_host_proc_init",
        "python",
        "try:\n d=open('/proc/1/cmdline','rb').read()\n "
        "print('ESCAPED' if (b'dockerd' in d or b'systemd' in d) else 'own-init')\n"
        "except Exception: print('blocked')",
    ),
    (
        "write_etc_hosts",
        "python",
        "try:\n open('/etc/hosts','w').write('evil')\n print('ESCAPED')\n"
        "except Exception: print('blocked')",
    ),
    (
        "write_root_dir",
        "python",
        "try:\n open('/owned','w').write('x')\n print('ESCAPED')\n"
        "except Exception: print('blocked')",
    ),
    (
        "chroot_escape",
        "python",
        "import os\ntry:\n os.chroot('/')\n print('ESCAPED')\nexcept Exception: print('blocked')",
    ),
    (
        "symlink_attack",
        "bash",
        "ln -s /etc/shadow /tmp/p 2>/dev/null; cat /tmp/p 2>/dev/null | grep -q 'root:' "
        "&& echo ESCAPED || echo blocked",
    ),
    (
        "exec_from_tmp_noexec",
        "bash",
        "cp /bin/echo /tmp/e 2>/dev/null && chmod +x /tmp/e 2>/dev/null && /tmp/e ESCAPED || echo blocked",
    ),
    # ── Network escapes ───────────────────────────────────────────────────────────────────
    (
        "tcp_connect_external",
        "python",
        "import socket\ntry:\n s=socket.socket(); s.settimeout(3); s.connect(('1.1.1.1',53))\n"
        " print('ESCAPED')\nexcept Exception: print('blocked')",
    ),
    (
        "udp_send",
        "python",
        "import socket\ntry:\n s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        " s.sendto(b'x',('1.1.1.1',53)); print('ESCAPED')\nexcept Exception: print('blocked')",
    ),
    (
        "http_request",
        "python",
        "import urllib.request\ntry:\n urllib.request.urlopen('http://example.com',timeout=3)\n"
        " print('ESCAPED')\nexcept Exception: print('blocked')",
    ),
    (
        "dns_lookup",
        "python",
        "import socket\ntry:\n socket.gethostbyname('example.com'); print('ESCAPED')\n"
        "except Exception: print('blocked')",
    ),
    # ── Privilege escalation ──────────────────────────────────────────────────────────────
    (
        "setuid_to_root",
        "python",
        "import os\ntry:\n os.setuid(0)\n print('ESCAPED' if os.getuid()==0 else 'no')\n"
        "except Exception: print('blocked')",
    ),
    (
        "ptrace_self",
        "python",
        "import ctypes\ntry:\n r=ctypes.CDLL(None).ptrace(0,0,0,0)\n "
        "print('ESCAPED' if r==0 else 'blocked')\nexcept Exception: print('blocked')",
    ),
    ("load_kernel_module", "bash", "modprobe xfs 2>/dev/null && echo ESCAPED || echo blocked"),
    (
        "write_sysfs",
        "bash",
        "echo 1 > /sys/kernel/uevent_helper 2>/dev/null && echo ESCAPED || echo blocked",
    ),
    # ── Resource attacks (must be contained, never crash the host) ──────────────────────────
    ("fork_bomb", "bash", ":(){ :|:& };: 2>/dev/null; echo done"),
    ("memory_bomb", "python", "x=bytearray(2*1024*1024*1024); print('ESCAPED', len(x))"),
    (
        "disk_fill",
        "bash",
        "dd if=/dev/zero of=/tmp/fill bs=1M count=5000 2>/dev/null; "
        "echo $(du -m /tmp/fill 2>/dev/null | cut -f1)MB",
    ),
    (
        "cpu_fork_spin",
        "python",
        "import os\nfor _ in range(200):\n try: os.fork()\n except Exception: pass\nprint('done')",
    ),
    # ── Docker / container-specific escapes ─────────────────────────────────────────────────
    (
        "docker_socket_access",
        "bash",
        "test -S /var/run/docker.sock && echo ESCAPED || echo no-socket",
    ),
    (
        "namespace_read_host",
        "bash",
        # PID 1 inside the container is the sandbox init, so it must share the container's network
        # namespace with the current process. Seeing a DIFFERENT (i.e. host) namespace would be an
        # escape. Compare the inode links literally (they contain regex metachars like '[' ']').
        "a=$(readlink /proc/1/ns/net 2>/dev/null); b=$(readlink /proc/self/ns/net 2>/dev/null); "
        'if [ -n "$a" ] && [ "$a" != "$b" ]; then echo ESCAPED; else echo same-or-blocked; fi',
    ),
    (
        "cgroup_release_agent",
        "bash",
        "echo x > /sys/fs/cgroup/release_agent 2>/dev/null && echo ESCAPED || echo blocked",
    ),
    ("mount_proc", "bash", "mount -t proc proc /mnt 2>/dev/null && echo ESCAPED || echo blocked"),
    ("unshare_userns", "bash", "unshare -Urm 2>/dev/null echo ESCAPED || echo blocked"),
]


def _language_of(attempt: tuple[str, str, str]) -> str:
    return attempt[1]


@pytest.mark.parametrize("name,language,code", ESCAPE_ATTEMPTS, ids=[a[0] for a in ESCAPE_ATTEMPTS])
async def test_escape_attempt_is_blocked(name, language, code, real_executor, require_language):
    require_language(language)
    request = ExecutionRequest(language=language, code=code, timeout_seconds=8)
    result = await real_executor.execute(request, f"escape-{name}")

    # 1) The dangerous operation never succeeded.
    assert SENTINEL not in result.stdout, f"escape '{name}' SUCCEEDED: stdout={result.stdout!r}"
    # 2) No host-secret content leaked.
    assert "root:x:0:0" not in result.stdout, f"escape '{name}' leaked host passwd"
    # 3) The job reached a terminal state — i.e. the host stayed responsive and the call returned.
    assert result.status in ("COMPLETED", "FAILED", "TIMEOUT", "KILLED")


@pytest.mark.integration
async def test_system_stays_responsive_after_attacks(real_executor, require_language):
    """After a barrage of malicious jobs, a normal job still succeeds (no host degradation)."""
    require_language("python")
    for name, language, code in ESCAPE_ATTEMPTS[:6]:
        if language == "python":
            await real_executor.execute(
                ExecutionRequest(language=language, code=code, timeout_seconds=5), f"barrage-{name}"
            )
    healthy = await real_executor.execute(
        ExecutionRequest(language="python", code="print('still-alive')", timeout_seconds=5),
        "health",
    )
    assert healthy.status == "COMPLETED"
    assert "still-alive" in healthy.stdout
