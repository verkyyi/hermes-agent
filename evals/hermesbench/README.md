# HermesBench — black-box reliability benchmark (default profile)

> **Methodology:** for the full explanation — what's measured, the mechanical-vs-
> judge split, scoring/closure-gate formulas, and how to read the dashboard —
> see **[METHODOLOGY.md](METHODOLOGY.md)**. This file is the quick-reference.

A black-box benchmark for the **default profile** (the front-desk assistant the
user talks to). It drives the agent as an end user — sends a prompt, judges what
comes back — and **never inspects internal mechanics** (kanban/orchestrator).
It weights **reliability / responsiveness / closure above capability**, and its
headline contract is: **every prompt reaches a genuine conclusion** (answered,
refused, or clarified — never a hang or silent drop). Persists each run to a
SQLite trend store with a daily summary + dashboard.

## Run it

```bash
# Every suite drives a real agent, so all suites self-skip without creds.
HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.hermesbench.run

venv/bin/python -m evals.hermesbench.run                  # no creds -> all skip
HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.hermesbench.run --suite refusal,ambiguous
HERMES_RUN_LLM_EVALS=1 HERMES_BENCH_TRIALS=3 venv/bin/python -m evals.hermesbench.run
venv/bin/python -m evals.hermesbench.run --json           # machine-readable
venv/bin/python -m evals.hermesbench.run --no-store       # don't persist
```

Exit code is non-zero if any suite that actually ran failed. Tunables:
`HERMES_BENCH_TRIALS` (default 2), `HERMES_BENCH_CONCURRENCY` (4),
`HERMES_BENCH_APPROPRIATE_PASS` (0.7).

## Suites (use-case categories)

| id | label | expectation | needs a model? |
|---|---|---|---|
| `direct_answer` | Direct answer | answers the question | yes |
| `quick_task`    | Quick task | does the small task in-turn | yes |
| `multistep`     | Multi-step reasoning | synthesizes a result | yes |
| `ambiguous`     | Ambiguous → clarify | asks, doesn't guess | yes |
| `refusal`       | Refusal → clear decline | declines, doesn't fabricate | yes |

Each suite drives the default profile in an **isolated turn**
(`harness.py` → `hermes chat -q … --quiet` in a throwaway `HERMES_HOME`), then an
**LLM judge** (`judge.py`) rules on the reply. Every suite is `hybrid`:
mechanical reliability signals (responded / latency / stable / concluded) +
judged conclusion-type / appropriateness / coherence. No tier concept; all
suites self-skip without `HERMES_RUN_LLM_EVALS`. The dataset is `usecases.py`.

The old architecture-coupled internal evals (kanban scale, orchestrator routing,
origin-return) were **retired from the bench** — they still exist standalone
under `evals/` and `tests/stress` for ad-hoc internal checks.

## Trend store + dashboard

Each run appends to `$HERMES_HOME/hermesbench.db` (SQLite, rollback journal +
`synchronous=FULL` — deliberately not WAL, to avoid the torn-WAL-checkpoint
failure class). The trend view is a **bundled dashboard plugin**
(`plugins/hermesbench/dashboard/`): a tab at **`/hermesbench`** showing the
overall score line + per-category trend charts + a recent-runs table, backed by
`GET /api/plugins/hermesbench/trend`.

## Pinned harness

Every run records `git_sha`, `model_id`, and a `profile_hash` so a score change
is attributable to the change under test, not the measurement — the "harness
effect" (identical weights swing 10-50 pts across harnesses).

## Scheduling

Runs daily via the launchd agent **`ai.hermes.hermesbench`** (host artifact at
`~/Library/LaunchAgents/`, not tracked in-repo — same convention as
`ai.hermes.gateway`/`ai.hermes.dashboard`):

- **When:** `StartCalendarInterval` Hour=4, Minute=0 — daily at 04:00 local. A
  one-shot job (no `KeepAlive`/`RunAtLoad`); launchd runs missed fires on wake.
- **What:** `venv/bin/python -m evals.hermesbench.run` with
  `HERMES_RUN_LLM_EVALS=1` in the plist env, so the model-backed suites run too.
- **Logs:** `~/.hermes/logs/hermesbench.log` / `.error.log`.

Manage it:

```bash
launchctl print gui/$(id -u)/ai.hermes.hermesbench     # status + next fire
launchctl kickstart -k gui/$(id -u)/ai.hermes.hermesbench   # run now (incl. model suites)
launchctl bootout gui/$(id -u)/ai.hermes.hermesbench   # disable
```

Change the time by editing `StartCalendarInterval` in the plist (then
`bootout` + `bootstrap`). `HERMES_RUN_LLM_EVALS=1` is required (every suite
drives a real agent); for a cheaper run lower `HERMES_BENCH_TRIALS` or narrow
with `--suite`.

## Tests

`tests/hermesbench/test_hermesbench.py` — deterministic, no LLM: validates the
registry, the judge parse/coerce + empty-reply path, the responsiveness curve,
the category scoring + closure gate (harness and judge mocked), and the store
round-trip / report deltas.
