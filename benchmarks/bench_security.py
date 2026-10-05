"""Run the real-Docker integration + container-escape suites and record machine-readable results.

Requires built runtime images and a reachable Redis (REDIS_URL, db 15 is flushed by the tests).
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, RESULTS_DIR, environment, write_json


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    junit = RESULTS_DIR / "security_junit.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/security",
            "tests/integration",
            "-m",
            "integration",
            "-q",
            f"--junitxml={junit}",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    print(proc.stdout[-3000:])
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite[0]
    cases = []
    for case in suite.iter("testcase"):
        outcome = "passed"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[child.tag]
        cases.append(
            {
                "name": f"{case.get('classname')}::{case.get('name')}",
                "outcome": outcome,
                "time_s": float(case.get("time", 0)),
            }
        )
    counts = {
        k: sum(1 for c in cases if c["outcome"] == k)
        for k in ("passed", "failed", "error", "skipped")
    }
    write_json(
        "security_and_integration",
        {
            "benchmark": "security_and_integration",
            "definition": "pytest tests/security tests/integration -m integration against real "
            "containers; each escape payload prints ESCAPED only if it succeeded.",
            "environment": environment(),
            "counts": counts,
            "pytest_returncode": proc.returncode,
            "cases": cases,
        },
    )
    print(counts)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
