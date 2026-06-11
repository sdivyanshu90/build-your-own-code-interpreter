"""Unit tests for the seccomp profile builder and the static profile artifacts."""

from __future__ import annotations

import glob
import json
import os

import pytest

from worker.sandbox import seccomp

LANGUAGES = ["python", "javascript", "typescript", "java", "go", "ruby", "rust", "bash"]

# Syscalls that must be denied (absent from the allow-list) in every profile.
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
    return set(profile["syscalls"][0]["names"])


class TestSeccompProfiles:
    def test_default_deny_present_in_all_profiles(self):
        for lang in LANGUAGES:
            profile = seccomp.build_profile(lang)
            assert profile["defaultAction"] == "SCMP_ACT_ERRNO", lang

    def test_python_profile_allows_required_syscalls(self):
        allowed = _allowed(seccomp.build_profile("python"))
        for required in ("read", "write", "futex", "mmap", "brk", "rt_sigaction", "openat"):
            assert required in allowed, required

    def test_python_profile_denies_ptrace(self):
        assert "ptrace" not in _allowed(seccomp.build_profile("python"))

    def test_python_profile_denies_socket_when_network_disabled(self):
        allowed = _allowed(seccomp.build_profile("python", network_enabled=False))
        assert "socket" not in allowed
        assert "connect" not in allowed

    def test_network_syscalls_present_when_enabled(self):
        allowed = _allowed(seccomp.build_profile("python", network_enabled=True))
        assert "socket" in allowed and "connect" in allowed

    def test_bash_profile_is_most_restrictive(self):
        bash = len(_allowed(seccomp.build_profile("bash")))
        for lang in ("python", "javascript", "java", "go", "ruby", "rust", "typescript"):
            assert bash < len(_allowed(seccomp.build_profile(lang))), lang

    def test_java_profile_allows_clone_with_restrictions(self):
        profile = seccomp.build_profile("java")
        clone_rules = [s for s in profile["syscalls"] if s["names"] == ["clone"]]
        assert clone_rules, "no clone rule"
        args = clone_rules[0]["args"]
        assert args[0]["op"] == "SCMP_CMP_MASKED_EQ"
        assert args[0]["value"] == seccomp._CLONE_NAMESPACE_MASK
        assert args[0]["valueTwo"] == 0

    def test_clone3_returns_enosys(self):
        profile = seccomp.build_profile("go")
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
        allowed = _allowed(seccomp.build_profile(lang))
        leaked = [s for s in DANGEROUS if s in allowed]
        assert not leaked, f"{lang} leaked dangerous syscalls: {leaked}"

    def test_dangerous_set_is_subtracted_even_if_added(self):
        # Defensive guarantee: nothing in DANGEROUS_SYSCALLS can ever be allow-listed.
        for lang in LANGUAGES:
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
