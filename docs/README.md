# Handbook

Documentation for the Code Interpreter Sandbox, ordered for someone reviewing the project for the
first time. Everything here was checked against the code at the commit named in
[BENCHMARKS.md](BENCHMARKS.md); where older design prose disagreed with the code, the code won
and the discrepancy is recorded in [THREAT_MODEL.md section 5](THREAT_MODEL.md#5-implementation-status-of-controls-claimed-elsewhere).

## Suggested reading order

| # | Document | Read it to learn |
|---|---|---|
| 1 | [OVERVIEW.md](OVERVIEW.md) | What the system is, the problem, scope, known gaps, and a summary of the audit (bugs found and fixed). |
| 2 | [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request lifecycle and state machine diagrams (section 1.0), original design baseline. |
| 3 | [CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md) | Every directory and key function, in request order. |
| 4 | [SANDBOX.md](SANDBOX.md) | Exactly how one container is built and torn down; each isolation layer and its limits. |
| 5 | [THREAT_MODEL.md](THREAT_MODEL.md) | Assets, trust boundaries, threats with controls and residual risk; claims-vs-reality table. |
| 6 | [SECURITY.md](SECURITY.md) | Hardening guide, disclosure policy, production checklist. |
| 7 | [API.md](API.md) | Endpoints, schemas, WebSocket protocol, error catalogue with real examples. |
| 8 | [CONFIGURATION.md](CONFIGURATION.md) | Every environment variable and hard-coded limit. |
| 9 | [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) | ADR-style trade-offs. |
| 10 | [BENCHMARKS.md](BENCHMARKS.md) | Methodology, hardware, measured results, how to reproduce, limitations. |
| 11 | [TESTING.md](TESTING.md) | Test layers, how to run them, what each proves, what is not covered. |
| 12 | [DEPLOYMENT.md](DEPLOYMENT.md) | Running, sizing, lifecycle behaviour, host hardening. |
| 13 | [RUNBOOK.md](RUNBOOK.md) | Day-2 operations and incident procedures. |
| 14 | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptom-first fixes and FAQ. |
| 15 | [ADDING_LANGUAGE.md](ADDING_LANGUAGE.md) | Worked example: adding a runtime. |
| 16 | [GLOSSARY.md](GLOSSARY.md) | Terms used throughout. |

## By role

* **Reviewer / interviewer:** OVERVIEW, THREAT_MODEL section 5, BENCHMARKS, DESIGN_DECISIONS.
* **Operator:** DEPLOYMENT, CONFIGURATION, RUNBOOK, TROUBLESHOOTING.
* **Contributor:** CODE_WALKTHROUGH, TESTING, ADDING_LANGUAGE.
* **Security engineer:** SANDBOX, THREAT_MODEL, SECURITY.

## Conventions

* File paths are relative to the repository root.
* "Verified" means reproduced by running code in this repository; "read from code" means derived
  by reading it; "not tested" means exactly that.
* Benchmark numbers always come with the command that produced them and are in
  `benchmarks/results/`.
