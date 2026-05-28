# HermesBench — consolidated daily benchmark

One runner that wraps the fork's scattered eval harnesses behind a single
registry, persists every run to a SQLite trend store, and renders a daily
summary with deltas vs the prior run. Built to run every day on the local
default profile.

## Run it

```bash
# Full (core + live). Live suites run only if HERMES_RUN_LLM_EVALS is set,
# otherwise they're recorded as skipped (not failed).
venv/bin/python -m evals.hermesbench.run

venv/bin/python -m evals.hermesbench.run --tier core      # deterministic only (no LLM/creds)
venv/bin/python -m evals.hermesbench.run --tier live      # live suites only
venv/bin/python -m evals.hermesbench.run --suite responsiveness,kanban_scale
venv/bin/python -m evals.hermesbench.run --json           # machine-readable
venv/bin/python -m evals.hermesbench.run --no-store       # don't persist
```

Exit code is non-zero if any suite that actually ran failed.

## Suites

| id | category | mode | tier |
|---|---|---|---|
| `responsiveness` | Front-desk responsiveness | automated | core |
| `kanban_scale`   | Kanban kernel scale       | automated | core |
| `orchestrator`   | Orchestration & routing   | hybrid    | live |
| `origin_return`  | End-to-end task return    | llm_judge | live |

Suites *wrap* the existing harnesses (`evals/responsiveness`,
`tests/stress/test_benchmarks.py`, `evals/orchestrator_routing`,
`evals/origin_return`) — those still run standalone. `kanban_scale` runs the
stress benchmark in an isolated subprocess because it mutates `HERMES_HOME`.

Grading modes mirror ClawBench Core v1: `automated` (deterministic),
`llm_judge`, `hybrid`.

## Tiers

- **core** — deterministic, no LLM, no credentials. Daily-safe, free, ~2 min
  (the kanban subprocess seeds up to 10k tasks).
- **live** — real LLM on the local profile; gated by `HERMES_RUN_LLM_EVALS=1`.

## Trend store + dashboard

Each run appends to `$HERMES_HOME/hermesbench.db` (SQLite, rollback journal +
`synchronous=FULL` — deliberately not WAL, to avoid the torn-checkpoint failure
mode that corrupted `kanban.db`). The dashboard serves a self-contained trend
view at **`/hermesbench`** (overall score over time + per-suite table), backed
by `GET /api/hermesbench/trend`.

## Pinned harness

Every run records `git_sha`, `model_id`, and a `profile_hash` so a score change
is attributable to the change under test, not the measurement — the "harness
effect" (identical weights swing 10-50 pts across harnesses).

## Scheduling

Not wired yet — run manually or add a launchd job
(`ai.hermes.hermesbench`, mirroring `ai.hermes.gateway`/`ai.hermes.dashboard`)
later.

## Tests

`tests/hermesbench/test_hermesbench.py` — deterministic; validates the registry,
scoring, store round-trip, report deltas, and tier/error/skip handling. No LLM.
