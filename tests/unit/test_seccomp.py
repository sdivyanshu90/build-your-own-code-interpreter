"""Unit tests for the seccomp profile builder and the static profile artifacts.

Two profile models exist (see worker/sandbox/seccomp.py):
  * interpreted runtimes use a tight default-DENY allow-list (defaultAction SCMP_ACT_ERRNO);
  * toolchain-heavy runtimes (typescript/go/rust) use a default-ALLOW profile that still denies
    every dangerous syscall and namespace-creating clone (defaultAction SCMP_ACT_ALLOW).
Both must block the same escape/privilege syscalls — the tests assert that invariant for both.
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from worker.sandbox import seccomp

LANGUAGES = ["python", "javascript", "typescript", "java", "go", "ruby", "rust", "bash"]
ALLOWLIST_LANGUAGES = ["python", "javascript", "java", "ruby", "bash"]
BLOCKLIST_LANGUAGES = ["typescript", "go", "rust"]

# Syscalls that must be denied in EVERY profile, regardless of model.
DANGEROUS = [
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "kexec_load",
    "init_module",
    "finit_module",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "unshare",
    "setuid",
    "setgid",
]


def _allowed(profile: dict) -> set[str]:
    """The plain allow-list (only meaningful for default-deny / allow-list profiles)."""
    for rule in profile["syscalls"]:
        if rule["action"] == "SCMP_ACT_ALLOW" and "args" not in rule:
            return set(rule["names"])
    return set()


def _effectively_denied(profile: dict, syscall: str) -> bool:
    """True if the syscall is blocked, accounting for both profile models."""
    deny_names: set[str] = set()
    allow_names: set[str] = set()
    for rule in profile["syscalls"]:
        if rule["action"] == "SCMP_ACT_ERRNO":
            deny_names |= set(rule["names"])
        elif rule["action"] == "SCMP_ACT_ALLOW" and "args" not in rule:
            allow_names |= set(rule["names"])
    if syscall in deny_names:
        return True
    if profile["defaultAction"] == "SCMP_ACT_ERRNO":
        return syscall not in allow_names
    return False  # default-allow and not explicitly denied


class TestSeccompProfiles:
    def test_interpreted_profiles_are_default_deny(self):
        for lang in ALLOWLIST_LANGUAGES:
            assert seccomp.build_profile(lang)["defaultAction"] == "SCMP_ACT_ERRNO", lang

    def test_toolchain_profiles_are_default_allow_with_denies(self):
        for lang in BLOCKLIST_LANGUAGES:
            profile = seccomp.build_profile(lang)
            assert profile["defaultAction"] == "SCMP_ACT_ALLOW", lang
            # It must carry an explicit deny rule for the dangerous syscalls.
            deny = [r for r in profile["syscalls"] if r["action"] == "SCMP_ACT_ERRNO"]
            assert deny, f"{lang} has no deny rule"

    def test_python_profile_allows_required_syscalls(self):
        allowed = _allowed(seccomp.build_profile("python"))
        for required in ("read", "write", "futex", "mmap", "brk", "rt_sigaction", "openat"):
            assert required in allowed, required

    def test_python_profile_denies_ptrace(self):
        assert _effectively_denied(seccomp.build_profile("python"), "ptrace")

    def test_python_profile_denies_socket_when_network_disabled(self):
        allowed = _allowed(seccomp.build_profile("python", network_enabled=False))
        assert "socket" not in allowed
        assert "connect" not in allowed

    def test_network_syscalls_present_when_enabled(self):
        allowed = _allowed(seccomp.build_profile("python", network_enabled=True))
        assert "socket" in allowed and "connect" in allowed

    def test_bash_profile_is_default_deny_and_blocks_network(self):
        # bash uses the shared default-deny allow-list; with no egress it must not expose sockets,
        # and (like every profile) must deny the dangerous syscalls.
        profile = seccomp.build_profile("bash")
        assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
        allowed = _allowed(profile)
        assert "socket" not in allowed and "connect" not in allowed
        for dangerous in ("ptrace", "mount", "setuid"):
            assert _effectively_denied(profile, dangerous)

    def test_java_profile_allows_clone_with_restrictions(self):
        profile = seccomp.build_profile("java")
        clone_rules = [s for s in profile["syscalls"] if s["names"] == ["clone"]]
        assert clone_rules, "no clone rule"
        args = clone_rules[0]["args"]
        assert args[0]["op"] == "SCMP_CMP_MASKED_EQ"
        assert args[0]["value"] == seccomp._CLONE_NAMESPACE_MASK
        assert args[0]["valueTwo"] == 0

    def test_blocklist_denies_namespace_creating_clone(self):
        # Default-allow profiles must still block clone() with any CLONE_NEW* flag.
        profile = seccomp.build_profile("rust")
        clone_denies = [
            s
            for s in profile["syscalls"]
            if s["names"] == ["clone"] and s["action"] == "SCMP_ACT_ERRNO"
        ]
        assert len(clone_denies) == len(seccomp._CLONE_NAMESPACE_FLAGS)

    def test_clone3_returns_enosys_in_both_models(self):
        for lang in ("go", "python"):  # one blocklist, one allowlist
            profile = seccomp.build_profile(lang)
            clone3 = [s for s in profile["syscalls"] if s["names"] == ["clone3"]]
            assert clone3 and clone3[0]["action"] == "SCMP_ACT_ERRNO"
            assert clone3[0]["errnoRet"] == 38

    def test_all_profiles_are_valid_json(self):
        for path in glob.glob(os.path.join(_runtimes_dir(), "*", "seccomp-profile.json")):
            with open(path) as handle:
                json.load(handle)  # raises on invalid JSON

    def test_all_profiles_pass_seccomp_schema_validation(self):
        for lang in LANGUAGES:
            profile = seccomp.build_profile(lang)
            assert isinstance(profile["defaultAction"], str)
            assert isinstance(profile["architectures"], list) and profile["architectures"]
            assert isinstance(profile["syscalls"], list) and profile["syscalls"]
            for rule in profile["syscalls"]:
                assert isinstance(rule["names"], list) and rule["names"]
                assert rule["action"].startswith("SCMP_ACT_")

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_dangerous_syscalls_denied_in_all_profiles(self, lang):
        profile = seccomp.build_profile(lang)
        leaked = [s for s in DANGEROUS if not _effectively_denied(profile, s)]
        assert not leaked, f"{lang} leaked dangerous syscalls: {leaked}"

    def test_dangerous_set_is_subtracted_from_allow_lists(self):
        # Defensive guarantee: nothing in DANGEROUS_SYSCALLS can ever be allow-listed.
        for lang in ALLOWLIST_LANGUAGES:
            allowed = seccomp.allowed_syscalls(lang)
            assert allowed.isdisjoint(seccomp.DANGEROUS_SYSCALLS), lang

    def test_static_artifacts_match_builder_output(self):
        # The committed runtimes/<lang>/seccomp-profile.json must equal the builder's output.
        for lang in LANGUAGES:
            path = os.path.join(_runtimes_dir(), lang, "seccomp-profile.json")
            with open(path) as handle:
                committed = json.load(handle)
            expected = json.loads(json.dumps(seccomp.build_profile(lang), sort_keys=True))
            committed_sorted = json.loads(json.dumps(committed, sort_keys=True))
            assert committed_sorted == expected, f"{lang} profile drifted; run `make seccomp-regen`"


def _runtimes_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "runtimes"
    )
