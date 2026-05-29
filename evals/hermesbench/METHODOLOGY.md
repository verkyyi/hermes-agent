# HermesBench — benchmark methodology (v2)

This document explains *what* HermesBench measures, *how* each number is
produced, and *how to read* the results. For run commands and operations see
[README.md](README.md).

---

## 1. What it is, and the philosophy

HermesBench is a **black-box reliability benchmark for the default profile** —
the front-desk assistant the user actually talks to. It evaluates **only from
the end user's perspective**: send a prompt, observe what comes back. It never
inspects internal mechanics (kanban boards, orchestrator routing, task linking,
dispatch). Four principles, in priority order:

1. **Default-profile, end-user view.** Drive the agent like a user; judge the
   reply, nothing else.
2. **Architecture-agnostic.** No assertions about internals. You can rip out and
   replace kanban/orchestrator and this benchmark still measures the same
   user-facing contract. (The previous version inspected board state and broke
   whenever the architecture moved — that coupling is the thing we removed.)
3. **Reliability > capability.** *Does it always respond, stay stable, feel
   responsive, and reach closure* matters more than *can it solve a hard
   problem.* Capability is better measured by external benchmarks; this one is
   an operational-reliability tripwire.
4. **Every prompt reaches a conclusion.** Whatever the request — answered,
   refused, or clarified — the turn must terminate with a genuine conclusion.
   Never a hang, crash, or silent drop. **Closure is the headline contract.**

### The harness-effect principle

Identical model weights swing 10–50 points across measurement harnesses, so a
*trend* benchmark must keep the harness fixed and record what it was. Every run
stamps a **harness fingerprint** (git sha, model id, profile hash; see §7).

---

## 2. Structure

```
usecases (dataset) ─┐
harness (drive) ────┼─► suites (one per category) ─► runner ─► store ─► report / dashboard
judge (LLM verdict)─┘
```

- **`usecases.py`** — the dataset: prompts grouped into categories, each with an
  `expectation` (the closure the user should get).
- **`harness.py`** — drives the default profile in an isolated turn and returns
  the reply + mechanical signals.
- **`judge.py`** — an LLM rules on the parts only judgement can assess.
- **`suites/usecases.py`** — one `run()` per category: drive + judge across
  trials, aggregate to `{score, passed, metrics}`.
- **`registry.py`** — lists the categories as suites.
- **`run.py` / `store.py` / `report.py`** — execute, persist to a SQLite time
  series, render with deltas. The dashboard plugin renders the trend.

There is no tier concept. Every suite drives a real agent, so all suites
**self-skip** when `HERMES_RUN_LLM_EVALS` is unset.

---

## 3. Grading: mechanical core + LLM judge

A subtlety: "reliability > capability" and "embrace LLM judgement" are in mild
tension — the reliability signals (responded? how fast? crashed? concluded?) are
exactly what you want measured **deterministically**, not by a judge. So every
suite is **hybrid**:

- **Mechanical** (from the harness, deterministic): `responded`, time-to-first-
  answer (`ttfa_ms`), total latency (`ttlt_ms`), `stable` (no crash/timeout/
  error), `concluded` (a terminal reply arrived within budget).
- **LLM-judged** (from `judge.py`): `conclusion_type` ∈
  {`completed`, `rejected`, `clarification`, **`none`**}, `appropriate` (0–1,
  vs the case's expectation), `coherent` (0–1).

Closure is where the two meet: a genuine conclusion requires *both* a terminal
reply (mechanical) *and* the judge ruling it isn't a stall (`none`).

---

## 4. The black-box harness

For each case the harness runs, in its **own throwaway `HERMES_HOME`** (config +
creds copied from the default profile):

```
hermes chat -q "<prompt>" --quiet
```

It captures **stdout** (the reply) plus the per-home `telemetry.db` row
(`ttfa_ms`, `ttlt_ms`, status) and wall-clock. Isolation means runs never touch
real chats or the production board, and each run gets an unambiguous latency
row. (Same isolation pattern as `evals/responsiveness/run_live`.)

> **Isolation caveat.** This drives a single synchronous front-desk turn, so the
> "conclusion" is the **turn-terminal reply** (answer / refusal / clarification /
> clear acknowledgment). It does **not** exercise async worker-delegated closure
> (kanban → worker → async return), which needs a gateway-based harness — that's
> a known gap (§9). The prompts are chosen to be resolvable in one turn. `chat -q`
> also has no gateway pre-LLM fast-ack, so the responsiveness signal is *total
> time to the reply*, not the sub-second front-desk ack (that's a gateway
> property the standalone responsiveness eval covers).

---

## 5. The judge

`judge.py` calls `agent.auxiliary_client.call_llm` (which auto-resolves the
default profile's provider/model, so the judge runs on the user's own model
family). It's told the case's `expectation` and rules:

- `conclusion_type` — `completed` / `rejected` / `clarification` / `none`.
  **`none`** is the failure: a stall, an empty/dangling reply, an "I'll get back
  to you" with nothing, or an off-topic non-answer.
- `appropriate` (0–1) — how well the behaviour matches the expectation (e.g. an
  *ambiguous* prompt should get `clarification`, not an invented answer; an
  *unknowable* prompt should be `rejected`, not fabricated).
- `coherent` (0–1) — clear, on-topic, non-contradictory.

An empty reply is ruled `none` without a model call. If the judge model itself
errors, that trial's judged axis is left unscored (`judge_error`) rather than
blamed on the agent.

---

## 6. Use-case categories

Each category is a suite (and a per-category dashboard trend). `expectation`
drives the judge's appropriateness ruling:

| Category | Label | Expectation | Good closure |
|----------|-------|-------------|--------------|
| `direct_answer` | Direct answer | `answer` | answers the question (`completed`) |
| `quick_task` | Quick task | `task_done` | does the small task in-turn (`completed`) |
| `multistep` | Multi-step reasoning | `task_done` | synthesizes a result, doesn't just dispatch (`completed`) |
| `ambiguous` | Ambiguous → clarify | `clarify` | asks a focused question (`clarification`), doesn't guess |
| `refusal` | Refusal → clear decline | `refuse` | declines / states the limit clearly (`rejected`), doesn't fabricate |

Add cases by editing `usecases.py` (and a budget + label for a new category).

---

## 7. Scoring, verdict, and the fingerprint

**Per category** (over all its prompts × trials), weighting reliability far
above capability:

```
score = 100 · (0.40·closure_rate + 0.20·stable_rate
               + 0.15·responsiveness_mean + 0.25·appropriate_mean)
```

- `closure_rate` — fraction of trials reaching a genuine conclusion (mechanical
  concluded **and** judge ≠ `none`).
- `stable_rate` — fraction with no crash/timeout/error.
- `responsiveness_mean` — per trial, a time-to-reply score: 1.0 at/under the
  category's `reply_target_s`, decaying linearly to 0 at 3×. Uses telemetry
  `ttfa_ms` when present, else wall-clock (this one-shot harness has no gateway
  fast-ack, so it's total time to the single reply).
- `appropriate_mean` — judge appropriateness over trials with a valid verdict.

**Pass** is gated on reliability: `closure_rate == 1.0` **and**
`stable_rate == 1.0` **and** `appropriate_mean ≥ 0.7`. A correct-but-never-
concluding turn fails; closure is non-negotiable.

**Overall score** = weighted mean of the categories that ran (skipped/errored
drop out). **Run verdict** = no suite that ran failed. **Fingerprint** per run:
`git_sha`, `model_id`, `profile_hash` — so a score move is attributable to the
*change*, not the *measurement*.

Trials default to 2 per case (`HERMES_BENCH_TRIALS`); concurrency
`HERMES_BENCH_CONCURRENCY` (default 4). More trials = a steadier estimate on a
non-deterministic system, at more tokens/time.

---

## 8. Trend store + dashboard

Runs append to **`$HERMES_HOME/hermesbench.db`** (SQLite, rollback journal +
`synchronous=FULL`, deliberately not WAL — avoids the torn-WAL-checkpoint
failure class, independent of the kanban kernel's own WAL).

The dashboard tab (`/hermesbench`, a bundled plugin) shows a single **Overall
score** line, **per-category trend** charts (one per category above), and a
recent-runs table. A line needs ≥2 points to draw.

**Reading a move:** overall down + one category down → localized; open the run
JSON (`--json`) for that category's `metrics` (closure/stable/appropriate +
`failures` sample + `conclusion_types`). A category flips to skip → creds
missing. Overall moved but no category did → check the fingerprint.

---

## 9. Known limitations

- **Async-delegated closure is not tested.** The harness measures the single
  front-desk turn; tasks that delegate to a worker and return asynchronously
  need a gateway-based harness (future work). Prompts are chosen to conclude
  in-turn.
- **Non-deterministic.** Real agent + LLM judge → run-to-run variance. Use more
  trials and read the trend, not a single run.
- **No token / cost instrumentation.** Cost ≈ (cases × trials) agent turns +
  one judge call each; not a measured dollar figure.
- **Judge shares the user's model family** (cheap + representative, but some
  self-grading bias; the rubric + low temperature mitigate it).
- **Small dataset** — a tripwire, not a comprehensive capability eval. Grow
  `usecases.py` over time.

---

## 10. Extending it

Add a prompt to an existing category in `usecases.py`, or add a category:
(1) add cases with a new `category` + `expectation`, a `BUDGETS` entry, and a
`CATEGORY_LABELS` entry; (2) add a `run_<category>()` wrapper in
`suites/usecases.py`. The registry builds suites from `usecases.categories()`
automatically, and the store/report/dashboard pick it up — a new per-category
chart and table column appear with no further changes.

`tests/hermesbench/test_hermesbench.py` validates the registry, the judge parse/
coerce, the responsiveness curve, and the category scoring + closure gate (with
the harness and judge mocked — no real LLM) — extend it alongside any new suite.
