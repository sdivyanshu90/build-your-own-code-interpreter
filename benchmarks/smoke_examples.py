"""Capture real request/response pairs and a few invariants from a running stack.

Used to produce the examples in docs/API.md and to check behaviour claimed in the docs:
  * sync/async execution, error shapes (400/401/403/409), malformed JSON
  * health reports MinIO up
  * after jobs finish: the stream is empty (acked entries deleted) and the user's concurrency set
    is empty (worker releases slots)

    python benchmarks/smoke_examples.py --base http://localhost:8080 [--redis-container NAME]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_api import DEFAULT_SECRET, Client, make_jwt
from common import RESULTS_DIR, environment


def redis_cli(container: str, *args: str) -> str:
    return subprocess.run(
        ["docker", "exec", container, "redis-cli", *args],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--redis-container", default="code-sandbox-redis-1")
    parser.add_argument("--secret", default=DEFAULT_SECRET)
    args = parser.parse_args()
    token = make_jwt(args.secret, sub="example-user", tier="authenticated")
    client = Client(args.base, token)
    anon = Client(args.base, "")
    anon.headers.pop("Authorization")
    bad = Client(args.base, "not.a.jwt")
    out: dict[str, object] = {"environment": environment()}

    def rec(name: str, status: int, body: object) -> None:
        out[name] = {"http_status": status, "body": body}
        print(f"{name}: {status}")

    rec("health", *client.request("GET", "/v1/health"))
    rec(
        "sync_python",
        *client.request("POST", "/v1/execute", {"language": "python", "code": "print(6*7)"}),
    )
    rec(
        "validation_error",
        *client.request("POST", "/v1/execute", {"language": "cobol", "code": "x"}),
    )
    rec(
        "no_credentials",
        *anon.request("POST", "/v1/execute", {"language": "python", "code": "print(1)"}),
    )
    rec(
        "bad_token", *bad.request("POST", "/v1/execute", {"language": "python", "code": "print(1)"})
    )
    status, body = client.request(
        "POST", "/v1/execute/async", {"language": "bash", "code": "echo async"}
    )
    rec("async_submit", status, body)
    job = body["job_id"]
    for _ in range(100):
        status, rec_body = client.request("GET", f"/v1/jobs/{job}")
        if rec_body.get("status") in ("COMPLETED", "FAILED", "TIMEOUT", "KILLED"):
            break
        time.sleep(0.1)
    rec("job_record_terminal", status, rec_body)
    rec("cancel_terminal_job", *client.request("DELETE", f"/v1/jobs/{job}"))
    rec(
        "nonzero_exit",
        *client.request(
            "POST",
            "/v1/execute",
            {"language": "python", "code": "import sys; print('x'); sys.exit(3)"},
        ),
    )
    rec(
        "syntax_error",
        *client.request("POST", "/v1/execute", {"language": "python", "code": "def ("}),
    )
    rec(
        "stdin_and_files",
        *client.request(
            "POST",
            "/v1/execute",
            {
                "language": "python",
                "stdin": "abc\n",
                "files": [{"name": "data.txt", "content": "from file"}],
                "code": "import sys; print(sys.stdin.read().strip(), open('/sandbox/data.txt').read())",
            },
        ),
    )
    rec(
        "timeout_job",
        *client.request(
            "POST",
            "/v1/execute",
            {"language": "python", "timeout_seconds": 2, "code": "while True: pass"},
        ),
    )
    # malformed JSON through a raw connection
    client.conn.request("POST", "/v1/execute", body="{not json", headers=client.headers)
    resp = client.conn.getresponse()
    rec("malformed_json", resp.status, json.loads(resp.read() or b"{}"))

    time.sleep(2)
    out["invariants"] = {
        "xlen_sandbox_jobs_after_jobs": redis_cli(args.redis_container, "XLEN", "sandbox:jobs"),
        "xpending_summary": redis_cli(args.redis_container, "XPENDING", "sandbox:jobs", "workers"),
        "zcard_concurrency_example_user": redis_cli(
            args.redis_container, "ZCARD", "sandbox:concurrency:example-user"
        ),
    }
    print(out["invariants"])
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "smoke_examples.json").write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
