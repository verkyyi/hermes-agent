"""HermesBench — one consolidated, daily-runnable benchmark for the local profile.

Wraps the existing scattered eval harnesses (responsiveness, kanban scale,
orchestrator routing, origin-return) behind a single registry + runner, persists
every run to a SQLite trend store, and renders a daily summary with deltas vs a
rolling baseline.

Design notes:
  - Suites declare a *tier*: ``core`` (deterministic, no LLM, no creds) or
    ``live`` (real LLM on the local profile, gated by HERMES_RUN_LLM_EVALS).
  - Suites declare a *mode* mirroring ClawBench Core v1: ``automated`` (precise
    deterministic check), ``llm_judge``, or ``hybrid``.
  - The harness is pinned: every run records git sha, model id and a profile
    hash so a score change is attributable to the *change*, not the measurement
    (the "harness effect" — same weights swing 10-50pts across harnesses).

Entry point:
    venv/bin/python -m evals.hermesbench.run            # full (core + live)
    venv/bin/python -m evals.hermesbench.run --tier core
    venv/bin/python -m evals.hermesbench.run --json
"""
