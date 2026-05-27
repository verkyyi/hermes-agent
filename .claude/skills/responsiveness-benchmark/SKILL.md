---
name: responsiveness-benchmark
description: Run or extend the Hermes default-profile responsiveness benchmark — the deterministic pre-LLM-ack + public-progress-cadence policy benchmark, and the opt-in live time-to-first-token (TTFT) measurement over emulated user sessions. Use when the user asks to run the responsiveness benchmark, measure responsiveness / time-to-first-token / time-to-first-feedback / front-desk latency on the default profile, or add/extend responsiveness test cases.
---

# Responsiveness benchmark

Measures how fast a user on the **default front-desk profile** gets *visible
feedback*, across emulated real user sessions. It exercises the two gateway
policies that govern perceived responsiveness: the **pre-LLM ack** (an immediate
"on it" sent before model latency) and the **public long-progress** cadence
("still working…" during a long turn).

Code lives in the `hermes-agent` repo:
- `evals/responsiveness/` — `dataset.py` (sessions), `score.py` (rubric), `run.py` (deterministic CLI), `run_live.py` (live TTFT)
- `tests/responsiveness_benchmark/` — deterministic pytest suite + gated live smoke test

## How to run

Run from the **repo root**. If the code is in a git worktree (under
`.claude/worktrees/`), `cd` into that worktree first — the editable install
resolves packages from the current directory. Use the project venv python
(`venv/bin/python`, or `.venv/bin/python`).

### 1. Deterministic benchmark — no LLM, no credentials (start here, CI-safe)

```bash
venv/bin/python -m evals.responsiveness.run            # PASS/FAIL + metrics
venv/bin/python -m evals.responsiveness.run --rows     # + per-turn detail
venv/bin/python -m evals.responsiveness.run --json     # machine-readable
venv/bin/python -m pytest tests/responsiveness_benchmark/   # 28 GREEN + 2 TDD xfails
```

It drives the real gateway ack/progress decision functions over the session
dataset and scores **time-to-first-feedback (TTFF)** with a latency model.
Exits non-zero on a threshold regression. The headline: a long-work turn's TTFF
drops from a ~90s silence to ~0.3s because the ack fires.

### 2. Live TTFT — real model calls, opt-in (needs provider credentials)

Gated behind `HERMES_RUN_LLM_EVALS=1`. Reads the agent's own measured
`ttft_ms`/`ttfa_ms`/`ttlt_ms` from telemetry. Each turn runs in an isolated temp
`HERMES_HOME` (config + creds copied from the default profile).

```bash
# Default: full real toolset, concurrency 8, safe turns only
HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.responsiveness.run_live --only-daily

# MOST ACCURATE number (one request at a time, like a real session):
HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.responsiveness.run_live \
    --only-daily --concurrency 1 --trials 5

# Gated smoke test:
HERMES_RUN_LLM_EVALS=1 venv/bin/python -m pytest \
    tests/responsiveness_benchmark/test_live_ttft.py -m integration
```

Flags: `--tools {none,safe,full}` (default `full`), `--concurrency N`
(default 8), `--trials N`, `--only-daily`, `--json`, and — only to run the
destructive `tool_safe=False` turns — `--include-unsafe --allow-side-effects`.
Env overrides: `HERMES_RESP_LIVE_CONCURRENCY`, `HERMES_RESP_LIVE_TIMEOUT_S`,
`HERMES_RESP_ALLOW_SIDE_EFFECTS=1`, `HERMES_RESP_SHORT_REPLY_LATENCY_S`.

## Interpreting results

- **TTFT is measured before any tool runs** — tools add to total latency (TTLT),
  never lower TTFT. Do not read a lower TTFT as a "tools made it faster" effect.
- **`--concurrency N>1` inflates absolute TTFT** (concurrent calls contend for
  provider capacity). It's a *speed* knob; for the faithful number use
  `--concurrency 1` plus several `--trials`. High concurrency can also hit
  provider rate limits.
- **p95 needs samples.** At ~1 sample/turn, "p95" is just the worst single
  sample; the runner prints a small-n caveat. Raise `--trials`.
- **`--tools full` uses REAL tools + REAL account credentials.** It runs only
  the `tool_safe` turns; destructive turns require explicit confirmation.

## Extending

Add sessions/turns in `evals/responsiveness/dataset.py`. Each turn needs
`kind` (`trivial`/`short`/`long_work`/`command`), `expect_ack` (the ideal),
optional `known_gap: True` for a forward-looking [TDD] target, `tool_safe` for
live `full` runs, and for `long_work`: `run_seconds` + `phases`. After editing,
re-run the deterministic suite; the scorer and tests pick up new turns
automatically. Calibrate the deterministic latency model from a measured live
p50 via `HERMES_RESP_SHORT_REPLY_LATENCY_S`.
