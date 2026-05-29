"""HermesBench — black-box reliability benchmark for the default profile.

Drives the default-profile agent as an end user would (isolated `hermes chat -q`
turns), judges the replies with an LLM, and scores reliability/responsiveness/
closure above capability. It never inspects internal mechanics (kanban,
orchestrator) — it's architecture-agnostic on purpose. Persists every run to a
SQLite trend store with a daily summary + dashboard.

Design notes:
  - Black-box, default-profile, end-user perspective only.
  - Reliability > capability; **every prompt must reach a genuine conclusion**
    (answer / refusal / clarification) — closure is the headline contract.
  - Hybrid grading: mechanical reliability signals (responded / latency /
    stable / concluded) + an LLM judge (conclusion-type / appropriate /
    coherent). No tier concept; all suites drive real agents and self-skip
    without HERMES_RUN_LLM_EVALS.
  - Harness pinned: each run records git sha, model id, profile hash (the
    "harness effect" — same weights swing 10-50pts across harnesses).
  - The old architecture-coupled internal evals (kanban scale, orchestrator
    routing, origin-return) were retired from the bench; they remain standalone
    under evals/ and tests/stress.

Entry point:
    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.hermesbench.run   # all use cases
    venv/bin/python -m evals.hermesbench.run --suite refusal,ambiguous
    venv/bin/python -m evals.hermesbench.run --json
"""
