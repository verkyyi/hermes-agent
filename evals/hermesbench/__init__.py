"""HermesBench — one consolidated, daily-runnable benchmark for the local profile.

Wraps the existing scattered eval harnesses (responsiveness, kanban scale,
orchestrator routing, origin-return) behind a single registry + runner, persists
every run to a SQLite trend store, and renders a daily summary with deltas vs a
rolling baseline.

Design notes:
  - Every suite runs on every invocation — there is no tier concept. The
    model-backed suites (orchestrator, origin_return) self-skip when
    HERMES_RUN_LLM_EVALS is unset, so a creds-less run degrades cleanly to the
    deterministic suites.
  - Suites declare a *mode* mirroring ClawBench Core v1: ``automated`` (precise
    deterministic check), ``llm_judge``, or ``hybrid``.
  - The harness is pinned: every run records git sha, model id and a profile
    hash so a score change is attributable to the *change*, not the measurement
    (the "harness effect" — same weights swing 10-50pts across harnesses).

Entry point:
    venv/bin/python -m evals.hermesbench.run            # all suites
    venv/bin/python -m evals.hermesbench.run --suite responsiveness,kanban_scale
    venv/bin/python -m evals.hermesbench.run --json
"""
