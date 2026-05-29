# HermesBench — benchmark methodology

This document explains *what* HermesBench measures, *how* each number is
produced, and *how to read* the results. For run commands and operations see
[README.md](README.md).

---

## 1. What it is, and why

Hermes accumulated several separate eval harnesses over time — front-desk
responsiveness, Kanban kernel scale, orchestrator routing, end-to-end task
return. Each was run by hand, ad hoc, with no shared score and no history.

**HermesBench consolidates them into one runner that produces a single,
comparable score every day and stores it as a time series.** The goal is not a
leaderboard number for its own sake; it's a *regression tripwire* for the local
deployment: if a change quietly degrades responsiveness, orchestration accuracy,
or kernel latency, the daily score moves and you see it on the dashboard.

### The harness-effect principle

A central lesson from 2026 agent-eval research is that the **measurement
harness, not just the model, determines the score** — identical model weights
swing 10–50 points across different harnesses. The implication for a *trend*
benchmark is strict: **the harness must stay fixed, and every run must record
what it was.** A score drop should be attributable to the change under test, not
to a change in how we measured. HermesBench therefore stamps every run with a
**harness fingerprint** (see §5) and avoids silently altering scoring logic.

---

## 2. Structure

```
registry  →  suites  →  runner  →  store  →  report / dashboard
```

- **registry** (`registry.py`) declares the suites and their metadata: id,
  category, grading **mode**, and **weight**.
- **suites** (`suites/*.py`) are thin adapters. Each wraps an existing harness
  and returns a normalized `{score: 0–100, passed: bool, metrics: {...}}`
  (or `{skipped: True, skip_reason}`). They do not re-implement the underlying
  evals — they call them and normalize the result.
- **runner** (`run.py`) runs every registered suite (or a `--suite a,b` subset),
  executes each with timing + error capture, computes the weighted overall, and
  writes the run.
- **store** (`store.py`) appends the run to a SQLite time series.
- **report** (`report.py`) renders a text summary with deltas vs the prior run;
  the **dashboard plugin** renders the trend visually.

There is no "tier" concept. Every suite runs on every invocation. The
model-backed suites self-gate (next section), so a credential-less run still
works — it just records those suites as skipped.

---

## 3. Grading modes

Mirroring ClawBench Core v1's design, each suite declares how it grades:

- **automated** — a precise, deterministic check (policy decision, latency
  threshold). No model, no credentials; reproducible bit-for-bit.
- **llm_judge** — outcome judged by a real agent run / model behaviour.
  Non-deterministic by nature.
- **hybrid** — deterministic structural checks combined with model-driven
  behaviour.

| Suite | Category | Mode | Weight | Needs a model? |
|-------|----------|------|--------|----------------|
| `responsiveness` | Front-desk responsiveness | automated | 1.0 | no |
| `kanban_scale`   | Kanban kernel scale       | automated | 0.7 | no |
| `orchestrator`   | Orchestration & routing   | hybrid    | 1.0 | yes |
| `origin_return`  | End-to-end task return    | llm_judge | 0.8 | yes |

**Self-gating instead of tiers.** The two model-backed suites (`orchestrator`,
`origin_return`) check `HERMES_RUN_LLM_EVALS` themselves and return *skipped*
when it isn't set. So a run with no credentials degrades cleanly to the two
deterministic suites — no tier flag required. The daily launchd job sets
`HERMES_RUN_LLM_EVALS=1`, so it runs all four; an ad-hoc local run without the
env var runs the two free ones. Use `--suite responsiveness,kanban_scale` to
force a deterministic-only subset explicitly.

---

## 4. The suites in detail

Each suite yields a **0–100 score** and a **pass/fail**. The score feeds the
trend; the pass/fail feeds the run verdict (§5).

### 4.1 `responsiveness` — front-desk responsiveness (automated)

**Wraps** `evals/responsiveness` — the deterministic pre-LLM-ack + public
progress-cadence policy benchmark. It drives the real gateway decision functions
over ~60 emulated user turns (trivial / short / long-work / command, DM vs
group) with ground-truth expectations. No model, no DB.

**What it checks**
- *Ack accuracy* — for every turn, did the gateway make the correct
  acknowledge / don't-acknowledge decision?
- *False-ack rate* — did it ever ack a trivial / short / command turn it
  shouldn't have?
- *Time-to-first-feedback (TTFF)* on long-work turns, modeled from calibrated
  latencies (`ACK_LATENCY_S`=0.3 s for acked turns).
- *Progress cadence* — for long-running turns, is the public progress-notice
  cadence well-formed (first notice timing, re-notice interval)?

**Score** = `100 × (0.4·ack_accuracy + 0.3·(1 − false_ack_rate) + 0.3·cadence_ok_rate)`

**Pass** mirrors the underlying benchmark's own thresholds, all of which are
strict:

| Metric | Threshold |
|--------|-----------|
| ack_accuracy | = 1.0 (every decision correct) |
| false_ack_rate | = 0.0 (never spam) |
| long_work p95 TTFF | ≤ 0.301 s (sub-second feedback on acked long turns) |
| cadence_ok_rate | = 1.0 |

### 4.2 `kanban_scale` — Kanban kernel scale (automated)

**Wraps** `tests/stress/test_benchmarks.py`, run in an **isolated subprocess**
(it mutates `HERMES_HOME` and `rmtree`s temp dirs, so it cannot run in-process
without clobbering the runner). It seeds boards at 100 / 1 000 / 10 000 tasks
and times kernel operations: `dispatch_once`, `recompute_ready`,
`build_worker_context`, `list_tasks`, `board_stats`, `list_runs` — 17 benches
total, median ms each.

**Score** = `100 × (benches under the ceiling / total benches)`,
where the ceiling is a deliberately generous **5 000 ms**
(`HERMES_BENCH_KANBAN_CEILING_MS`). **Pass** = worst median ≤ ceiling.

**Important nuance:** the absolute ceiling only catches *gross* (order-of-
magnitude) regressions — kernel latency is machine-dependent, so an absolute
pass/fail can't be tight. Finer drift is meant to be read from the **trend**:
the per-bench medians are stored, and a 2× slowdown shows as a falling line on
the dashboard even while the suite still "passes". Treat this suite's score as a
coarse gate and its stored medians as the real signal.

### 4.3 `orchestrator` — orchestration & routing (hybrid)

**Wraps** `evals/orchestrator_routing`. Spawns the **real orchestrator** over
**7 routing cases**, each on a throwaway **isolated** Kanban board (a temp
`HERMES_KANBAN_DB`, no real worker spawns, kanban toolset only). Default
**1 trial** per case (`HERMES_EVAL_TRIALS`), concurrency 3. Self-skips without
`HERMES_RUN_LLM_EVALS`.

The 7 cases: `web_research`, `ops_file_config`, `code_pr`, `light_synthesis`,
`report_briefing`, `multi_research_plus_ops`, `ambiguous_clarify`.

**What each case checks** (structural, deterministic on the resulting board):
- `routed` — work was decomposed into ≥1 subtask
- `self_completed` — the orchestrator's own task reached done/archived
- `linked` — direct children link back to the root
- `correct_assignee` — the expected worker profile(s) were targeted
- `single_subtask` (single-route) / `single_sink` + `all_converge` +
  `fanin_ok` (multi-route fan-in)
- *clarify* case inverts: an ambiguous request must end **blocked for
  clarification** with **no work created**
- notification-mode checks (`synthesize` / `leaves_silent`) apply only when the
  mode is observable; headless/unobservable never fails the case (the hybrid
  part — structure carries the verdict, mode strictness only *blocks* on an
  observed-wrong value)

**Score** = `100 × pass_rate` (fraction of case×trial attempts that pass all
their critical components). **Pass** = pass_rate ≥ **0.8**
(`HERMES_BENCH_ORCH_PASS`).

### 4.4 `origin_return` — end-to-end task return (llm_judge)

**Wraps** `evals/origin_return`. Two phases run with the real local profile:
- **phase A** — a front-desk turn must create a Kanban task *with the origin
  subscription attached* (so the eventual completion can return to the user).
- **phase B** — after the orchestrator self-parks (`kanban_decompose`), the
  return path must **survive on a non-done anchor** — a subscription left only
  on a done task means the user's answer can never come back.

**Score** = `100 × (phases passed / phases run)`. **Pass** = all run phases
passed. Phase B is reported as **skipped** (not failed) when the orchestrator
profile isn't installed. Self-skips without `HERMES_RUN_LLM_EVALS`.

> **Isolation caveat:** unlike `orchestrator` (fully isolated throwaway board),
> this suite exercises the real front-desk → kanban path and currently reads the
> **default local kanban board**. It therefore requires a healthy local
> `kanban.db` and is *not* safe against a corrupt one — a corrupt board makes it
> error out (this is how the 2026-05-28 corruption incident was surfaced). See
> Limitations (§8).

---

## 5. Aggregation, verdict, and the fingerprint

**Overall score** = the **weighted mean** of the scores of suites that actually
ran (skipped and errored suites drop out of both numerator and denominator).
Weights are in the table in §3.

**Run verdict (`passed`)** = *no suite that ran failed.* An errored suite counts
as a failure; a skipped suite does not. So a run with no credentials (the two
model suites skipped) can still pass on its deterministic suites alone.

**Harness fingerprint** — every run records:
- `git_sha` — the checkout the run executed from
- `model_id` — resolved from the local profile config (e.g. `gpt-5.5`)
- `profile_hash` — SHA-256 of the profile `config.yaml`

This is what makes day-over-day comparison meaningful: when the score moves, you
can tell whether the *code*, the *model*, or the *profile* changed underneath
it.

---

## 6. The trend store

Runs append to **`$HERMES_HOME/hermesbench.db`** (SQLite): a `runs` table
(run_id, ts, overall_score, passed, suites_ran, fingerprint) and a
`suite_results` table (per-suite score, passed, skipped, error, duration, and a
JSON `metrics` blob).

It uses a **rollback journal with `synchronous=FULL`, deliberately not WAL.**
Writes are once-daily and single-process, so WAL buys nothing — and the failure
mode it avoids (a torn WAL checkpoint under concurrent writers) is the same class
that corrupted the gateway's `kanban.db` on 2026-05-27. A conservative journal
removes that risk for this store entirely, independent of how the kanban kernel
manages its own WAL.

---

## 7. Reading the dashboard

The trend tab (`/hermesbench`) renders three things:

1. **Overall score**: a single line — each run's weighted `overall_score` over
   time.
2. **Per-category trends**: one small line chart per suite, each showing that
   suite's score over time with its latest value. This is where you localize a
   regression — a drop in the overall line is explained by whichever
   per-category chart fell. A suite that skipped/errored on a given run simply
   has no point there.
3. **Recent runs table**: per-run overall and each suite's score (with skip/err
   markers).

A line needs ≥2 points to draw, so with few runs you'll see dots until history
accumulates.

### How to interpret a move
- **Overall down, one category down** → localized regression; read that suite's
  metrics in the run JSON (`--json`) or the table.
- **`kanban_scale` still "passing" but its line falling** → latency drift below
  the gross ceiling; compare stored per-bench medians.
- **A model suite shows skip / err** → `HERMES_RUN_LLM_EVALS` was unset, creds
  were missing, or (for `origin_return`) the local kanban board was unhealthy —
  not necessarily a model regression.
- **Overall moved but no category did** → check the fingerprint: a `model_id`
  or `profile_hash` change means the *measurement* moved, per §1.

---

## 8. Known limitations

- **`origin_return` is not fully isolated** from the production `kanban.db` (see
  §4.4) — it requires a healthy local board and errors on a corrupt one.
- **No token / cost instrumentation.** Cost of a model-backed run is described
  by its agent-run count (~9 turns) and wall-clock, not a measured dollar figure.
- **Kanban ceiling is coarse and machine-dependent.** The absolute pass/fail
  catches only ~order-of-magnitude regressions; rely on the median trend for
  drift.
- **Orchestrator default is 1 trial/case** → susceptible to single-run noise on
  a non-deterministic model. Raise `HERMES_EVAL_TRIALS` for a tighter estimate.
- **llm_judge / hybrid suites are non-deterministic** — a one-off dip may be
  variance, not regression; confirm with a second run before acting.
- **`profile_hash` only covers `config.yaml`**, not `.env` or installed skills.

---

## 9. Extending it

Add a suite by (1) writing `suites/<id>.py` exposing `run() -> {score, passed,
metrics}` (or `{skipped, skip_reason}`), and (2) registering it in
`registry.py` with its category, mode, and weight. If it needs a model, have its
`run()` return `{skipped: True}` when `HERMES_RUN_LLM_EVALS` is unset, mirroring
the existing model suites. The runner, store, report, and dashboard pick it up
automatically — the per-category chart and the table add a column with no
further changes. Keep heavy imports *inside* `run()` so a deterministic-only run
never pays for a model suite's dependencies.

`tests/hermesbench/test_hermesbench.py` validates the registry, scoring
normalization, store round-trip, report deltas, and error/skip handling — extend
it alongside any new suite.
