# Kanban Completion Delivery: Decompose-Anchor + Wake-Origin-Session

**Status:** Design / RFC (pre-implementation)
**Author:** Verky Yi (fork: `verky/deploy`)
**Date:** 2026-05-28
**Supersedes:** local patches #1–#4, #20 (the `notification_mode` / origin-provenance / gateway-synthesis cluster)
**Related upstream:** closed PR [NousResearch/hermes-agent#21523](https://github.com/NousResearch/hermes-agent/pull/21523)

---

## Goal

Replace the fork's gateway-side LLM **synthesis** path for Kanban completion
notifications with two composable primitives, both built on machinery that
already exists (most of it **upstream**):

1. **Decompose-anchor (`kanban_decompose` tool):** the orchestrator self-parks
   its own task as the durable fan-in **anchor** and re-wakes when its children
   finish — reusing upstream `decompose_triage_task`.
2. **Wake-origin-session (`_wake_origin_session`):** when the anchor task
   completes, re-enter its handoff into the *origin gateway session* as a
   synthetic inbound turn, so the origin profile composes and delivers the reply
   through the normal agent loop.

This fits the framework the maintainer asked for when closing #21523, deletes
~310 lines of gateway-baked LLM machinery, and collapses four fork-local schema
columns so `kanban_notify_subs` becomes upstream-identical.

## Why now

`teknium1` closed #21523 on architecture, not code quality (community reviewer
`TheoLong` reviewed end-to-end, 57/57 tests, fixed the one blocker):

> "Adding `notification_mode` + origin provenance + a synthesis path across
> gateway/tools/run_agent is substantive agent-orchestration design. We have
> the gateway-create auto-subscribe path (origin chat gets terminal-event
> notifications), plus the kanban-tool send_message bridge for cross-session
> delivery. **A third synthesis path would need to specify what it does that
> those two don't and how all three avoid drift.**"

The redesign answers directly: there is **no third path**. "Synthesis" is an
ordinary agent turn in the origin session (`_handle_message`, the same entry
point every inbound message uses), triggered by the anchor task completing.

---

## Background

### The two existing upstream delivery paths

1. **Passive poll-and-ping** (`gateway-create auto-subscribe`). The
   `_kanban_notifier_watcher` polls each board on a ~5s tick, claims unseen
   terminal events per subscription (`claim_unseen_events_for_sub`), renders a
   terse status line, and `adapter.send`s it. `notifier_profile` is **only** an
   anti-double-delivery filter.
2. **Active agent send** (`send_message` tool). Generic cross-channel delivery;
   already calls `mirror_to_session`.

Neither *wakes* a session: `mirror_to_session` (`gateway/mirror.py`) only
**appends durable transcript context** — it does not run a turn. "Worker
finishes → origin agent reacts" is the missing primitive, and teknium1's own
follow-up question names it: *"should worker completion become active
orchestrator-session context that triggers another orchestrator turn?"*

### What the fork carries today (to be replaced)

- **Schema (patches #1, #4):** five fork-local `kanban_notify_subs` columns —
  `notification_mode`, `origin_session_id`, `origin_profile`, `origin_context`,
  `request_id` — backfilled by idempotent `ALTER` guards (upstream ships only
  `notifier_profile`).
- **Gateway synthesis (patches #1, #2):** `gateway/kanban_synthesis.py`
  (`KanbanSynthesisMixin`, 574 lines) runs a no-tools aux-model rewrite over a
  hand-built prompt + gateway-read artifact excerpts, with a sanitized fallback,
  then `adapter.send` + `mirror_to_session`. TheoLong flagged the 45s blocking
  risk; the model can set `silent` on a user-visible task (footgun).
- **Orchestration gap:** the kanban-orchestrator skill has the orchestrator use
  plain `kanban_create(parents=...)` and **complete its own task immediately**
  after decomposing (`SKILL.md:154`). So the subscribed task (T0) completes with
  a "decomposed into…" non-answer, while the real answer is produced later by a
  downstream fan-in card whose completion (with parents) **does not** inherit the
  origin subscription (`_inherit_notify_sub_for_worker_root_task` only propagates
  to *parentless* worker roots). Net: the final answer can fail to return.

### Prior art we build on (already in the tree)

- **`decompose_triage_task` (UPSTREAM, `kanban_db.py:4042`).** Creates children,
  **links the root as a child of every leaf** so the root waits on the whole
  graph, flips the root to `todo` with `assignee=orchestrator`; when all children
  reach `done` the root re-promotes and *"its assignee wakes back up to judge
  completion or spawn more work."* This is the parked-anchor + re-wake model,
  upstream and test-covered.
- **`kanban_swarm.py` (fork) blackboard.** Uses the **root task as a shared
  blackboard** — structured `[swarm:blackboard] {key,value}` JSON comments via
  `post_blackboard_update` / `latest_blackboard` (last-write-wins per key), so
  "the dashboard, notifier, slash command, and dispatcher keep working without a
  new service." Validates "the orchestrator task is the central status."

---

## Converged design

### Topology (front-desk → orchestrator → workers)

```
User (gateway channel) ──ask──▶ Front-desk profile
   │ creates T0 (assignee=orchestrator); gateway-origin create is
   │ AUTO-SUBSCRIBED by default → origin sub lives on T0 and never moves
   ▼
[Kanban] T0 dispatched ──▶ Orchestrator (run #1, HERMES_KANBAN_TASK=T0)
   │ designs the graph itself, then calls kanban_decompose(children=[...])
   │   → reuse decompose_triage_task: create C1..Cn, link T0 as child of
   │     every leaf, T0: running → todo (parked anchor), end run #1
   │ orchestrator process EXITS (does NOT complete T0)
   ▼
[Kanban] C1..Cn dispatched ──▶ Workers (silent; no own subscriptions)
   │ each kanban_complete ──▶ child `completed` ──▶ notify NOBODY
   │ (optional) post_blackboard_update(T0, "child:Ci", "done") for live status
   ▼
[Kanban] all children done ──▶ T0 re-promotes (its "parents" = children all done)
   ▼
Orchestrator (run #2 on T0) ──▶ judges/aggregates (or spawns more)
   │ kanban_complete(T0, <final answer>)
   ▼
T0 `completed` + origin sub on T0 ──▶ _wake_origin_session ONCE
   ▼
Front-desk (warm session) runs a turn ──▶ reply delivered to the user
```

Key invariant: **one tracking task (T0) = one subscription = one wake**,
regardless of fan-out width or DAG depth. Child completions never wake the
gateway; they only move the dependency graph (and optionally update the
blackboard).

### Primitive 1 — `kanban_decompose` (self-park + re-wake)

Expose upstream `decompose_triage_task` as a model tool the **orchestrator**
calls on its own `HERMES_KANBAN_TASK`. The orchestrator (a full agent) supplies
the child graph it designed — we do **not** invoke the CLI's auxiliary-LLM
decomposer.

```
kanban_decompose(children=[{title, body, assignee, parents}, ...])
  → decompose_triage_task(root=<my task>, children=..., root_assignee=<me>)
  → my task: running → todo, linked as child of all leaves, current run ended
```

Two generalizations off the current CLI path:
1. **Relax the triage-status precondition + tear down run/claim state.** The
   guard lives in the **kernel function itself** (`kanban_db.py:4139` —
   `if status != "triage": return None`), not just the CLI caller, so this is an
   edit to upstream code (relax the guard or add a `self_park`/`from_status`
   param). Crucially, the function only flips `status='todo'` (`:4199`) — it does
   **not** clear `claim_lock`/`worker_pid`/`current_run_id` or close the open
   `task_run` that a *running* task has (triage tasks have none). Self-park must
   therefore also **end the current run + release the claim**, or
   `detect_crashed_workers` will later flag the parked anchor as a crashed worker
   (live claim, no process — patch #5 hazard). The linking + `recompute_ready`
   *mechanism* is status-agnostic (verified: root linked as child of every child
   `:4192`; `recompute_ready` promotes when all parents done `:2426`;
   `complete_task` triggers it per child completion `:3459`) — it's the run/claim
   teardown, not the graph logic, that the generalization must add.
2. **Skip the aux LLM.** The orchestrator profile reasons about the graph itself
   (per the kanban-orchestrator skill); the tool is a thin wrapper over the
   kernel primitive, fed by the orchestrator's own decomposition.

Lifecycle: T0 has **two runs** — run #1 = decompose+park (no completion), run #2
= aggregate+complete (after children done). `task_runs` already supports
multiple runs per task.

> The orchestrator must **not** call `kanban_complete` in run #1 — that would
> fire the wake prematurely with a "decomposed" non-answer.

### Primitive 2 — `_wake_origin_session` (gateway delivery)

`GatewayRunner._wake_origin_session(sub, event, task, board, direct_message)`:
build a `SessionSource` from the sub's routing identity, wrap a synthetic
`MessageEvent(internal=True)` carrying the handoff, and `await
self._handle_message(event)`. This is the pattern already proven by
`gateway/run.py::_process_handoff` (`internal=True` skips auth; the synthetic
turn persists into the origin transcript). Fires **only** on `kind ==
"completed"` for a `synthesize`-mode delivery.

Deletes all gateway-side LLM machinery in `gateway/kanban_synthesis.py`
(`_synthesize_kanban_notification`, `_build_kanban_synthesis_prompt`,
`_read_kanban_artifact_context`, `_sanitize_kanban_public_text`,
`_kanban_public_completion_fallback`, `_kanban_synthesis_route`,
`_kanban_synthesis_timeout`, `_coerce_kanban_metadata`). Keeps
`_send_kanban_notification` (delivery / `SendResult`-failure / mirror wrapper)
plus the new `_wake_origin_session`. **Prototype: 574 → 266 lines (net −310),
compiles clean, no non-test source references the deleted methods** (branch
`worktree-kanban-wake-primitive`).

### T0 as central status + blackboard progress roll-up

Because the subscription lives on T0 for the whole request, the gateway watches
exactly one task. Reuse the swarm blackboard so child progress rolls up onto T0:
children (or the dispatcher) call `post_blackboard_update(T0, "child:Ci",
"done")`; the front-desk reads `latest_blackboard(T0)` to render live status
("2/3 lanes done") from the single anchor — no extra subscriptions, no new
tables (state lives in `task_comments`).

### Aggregation path: A (default) vs B (future opt-in — not config)

Who turns the finished children into the final answer has two possible paths:

- **Path A — orchestrator re-wakes (run #2). DEFAULT, v1.** The anchor re-promotes
  when children finish; the orchestrator runs again, *judges* completion (it may
  spawn more work), and writes the aggregate as the anchor's completion. This is
  exactly what upstream `decompose_triage_task` gives us for free, and it is a
  **superset** — it always produces a correct result, including for dynamic DAGs.
- **Path B — front-desk aggregates on the wake.** Skip run #2; "all children
  done" wakes the front-desk, which reads the child handoffs (it has the kanban
  toolset, see Open Decision #5) and combines them in the warm turn. Only valid
  when the fan-out is **terminal/static** (no further orchestration needed).

This is deliberately **not a global config toggle.** The choice is
*workload-dependent, not deployment-dependent*: within one deployment, "summarize
these 3 lookups" wants B while "investigate X, then decide what to dig into next"
needs A. The only actor that knows is the **orchestrator at decompose time**, so
if B is ever added it is a per-`kanban_decompose` option the orchestrator sets
(e.g. `final_aggregation="self"` default vs `"origin"`), never an operator flag.
Path B is also strictly more mechanism (it needs an anchor-completes-without-a-run
trigger), so v1 ships **Path A only**; the cold re-dispatch it costs is an
*internal* hop — the user-visible final reply is the warm front-desk wake either
way.

### Schema collapse (→ upstream-identical)

The wake resolves its target **from the sub's routing identity** and re-enters
the *live* session, so origin-provenance columns are no longer load-bearing:

| Column | Decision | Rationale |
|---|---|---|
| `notification_mode` | **drop → config** | operator policy, not a per-task model field (removes the `silent` footgun) |
| `origin_session_id` | **drop** | session resolves from source, like any inbound message |
| `origin_profile` | **drop** | profile is encoded in the session_key (`agent:<profile>:…`) |
| `origin_context` | **drop** | the woken live session already holds the conversation |
| `request_id` | keep only for telemetry correlation; else drop | not needed for delivery |

`kanban_notify_subs` reverts to upstream's shape (`notifier_profile` only), so
patch #4's `ALTER` guards essentially disappear.

### Delivery mode → config (not a column, not model-set)

```yaml
kanban:
  notify:
    mode: direct            # global default
    telegram:
      mode: synthesize      # per-platform override
```

Precedence: env > per-platform > global > `direct`. **Constraint:**
`synthesize`/wake requires a **live gateway platform** (connected adapter +
running session loop). CLI/cron origins clamp to `direct` (the watcher already
only iterates connected adapters). The `notifier_profile` ownership guard is
unchanged: only the gateway owning the origin chat's adapter wakes the session.

### `mirror_to_session` dropped

The woken turn persists itself as a real assistant turn, so the explicit
post-send mirror call is removed. The completion reply becomes genuine
conversation history (follow-ups like "expand on that" work) instead of a
synthetic mirror record.

---

## When the wake fires

Only on `completed`, for a `synthesize`-resolved delivery, on the subscribed
anchor task:

| Path | Wakes session? | Behavior |
|---|---|---|
| Anchor T0 `completed` (Telegram-origin) | **yes** | warm front-desk turn → reply |
| `direct` mode (CLI / cron / explicit) | no | terse status line |
| child completions (fan-out workers) | no | silent; drive dependency graph / blackboard |
| `synthesize` + `blocked`/`gave_up`/`crashed`/`timed_out` | no | friendly public status line, sent directly |
| upstream `/kanban create` slash auto-sub | no | `direct` |

`blocked` waking the front-desk (to relay a clarification whose answer
naturally unblocks the task) is a plausible *future* extension; v1 is
`completed`-only.

---

## End-user impact

**No change** for `direct` / `silent` / CLI / cron — status pings are identical.

**Default Telegram orchestration flow:** completion replies become richer and
in-context (full session history, profile persona, tools), saved as real turns,
and the user can see live progress from T0's blackboard. Behavior changes to
manage: completions trigger a full front-desk turn (warm, but a real turn);
artifact delivery ownership moves to the woken agent; a failure fallback must
keep the user from getting nothing.

---

## Why ephemeral + event-driven, not a held-open orchestrator

(Resolved during design — recording so reviewers don't re-litigate.) Holding the
orchestrator process open to await children was considered and rejected:

- **Worker-slot deadlock under nesting.** The dispatcher has a bounded
  concurrency cap (`max_spawn`, `gateway/run.py:5201`). A held-open orchestrator
  consumes a slot for the whole fan-out; orchestrators-spawning-orchestrators can
  fill `max_spawn` with waiters and starve the children they wait on. Parked
  tasks hold no slot → deadlock-free by construction.
- **Runtime caps.** Tasks are time-bounded (`max_runtime_seconds`); an unbounded
  wait blows the limit or forces disabling hung-worker detection (patch #5).
- **Crash-safety.** State lives in `kanban.db` (durable) with ephemeral workers;
  a held process loses supervision/aggregation context on any crash/deploy. This
  DB already corrupted once under a spawn burst — betting orchestration state on
  process memory is the wrong direction.

The decompose-anchor pattern delivers the same reactivity (react to child events
→ re-wake) with continuity in the DB, not memory. If cold re-dispatch latency
ever bites, a **warm worker pool** attacks latency without sacrificing
durability — a future lever, not v1.

---

## Open decisions (with proposed defaults)

1. **Self-park run/claim lifecycle — RESOLVED (verified).** Reuse
   `_end_run(conn, task_id, outcome="decomposed")` (`kanban_db.py:2240`): it
   closes the active run, clears run-level claim, and nulls `current_run_id`.
   Extend the existing `decompose_triage_task` status flip to also null the
   task-level `claim_lock`/`claim_expires`/`worker_pid` (the pattern
   `archive_task` and crash-recovery already use, `:5176`). Crucially,
   `detect_crashed_workers` only scans `WHERE status='running' AND worker_pid IS
   NOT NULL` (`:5087`), so flipping the anchor `running → todo` **alone** already
   dodges crash detection — the claim-clear is cleanliness, not correctness.
   Child-of-leaves links gate re-promotion. Low risk, all existing helpers.
2. **Wake failure fallback.** Background-dispatch the wake (don't block the ~5s
   watcher tick); advance the delivery cursor only after the turn **succeeds**;
   on failure, **send the direct `msg`** so a completion is never lost.
3. **Artifact dedup.** Suppress the watcher's `_deliver_kanban_artifacts` when the
   resolved mode is `synthesize`; let the woken agent own artifacts.
4. **Blackboard roll-up scope.** Who posts child progress to T0 — the child on
   completion, or the dispatcher? *Proposed:* dispatcher posts terminal child
   transitions (uniform, no worker cooperation needed); orchestrator may add
   semantic keys in run #2.
5. **Front-desk capability on the wake turn — RESOLVED (verified).** The deploy
   front-desk profile **explicitly enables the `kanban` toolset** — globally
   (`~/.hermes/config.yaml` `toolsets: [..., kanban]`, line 13) and per-platform
   (`platform_toolsets.telegram` line 604, `cli` line 585). So
   `_profile_has_kanban_toolset()` (`tools/kanban_tools.py:49`) returns True and
   the wake turn has the **full kanban surface** (`kanban_show`/`kanban_list`,
   etc.) plus ungated `read_file` (`toolsets.py:37`). No capability gap and no
   compensating mechanism needed: the front-desk can read the anchor task, drill
   into individual child handoffs, and read artifact files directly.
   Embedding the handoff in the synthetic prompt remains a **latency
   optimization** (the answer is in-hand without a tool round-trip), not a
   workaround for missing access.
6. **Internal trigger persistence.** The synthetic `[task completed…]` user turn
   persists in the transcript (like `_process_handoff`). *Proposed:* accept for
   v1 (clearly bracketed); revisit if it bothers users.

Burst coalescing — a worry under per-child subscriptions — largely **dissolves**
here: only the single anchor wakes, once.

---

## Migration & rollout

- **Schema:** no migration needed for an upstream PR — `kanban_notify_subs`
  reverts to upstream's shape. On existing fork DBs the extra columns become
  vestigial (left in place, unread). Patch #4 guards retired except any column
  consciously kept.
- **Config:** ships defaulting to `direct`; deploy sets
  `kanban.notify.telegram.mode: synthesize` to preserve current behavior.
- **Tools:** `tools/kanban_tools.py` stops reading/writing `notification_mode` +
  origin fields on auto-subscribe; drop the `notification_mode` tool-schema enum.
  Add `kanban_decompose`. Retire `_inherit_notify_sub_for_worker_root_task` (sub
  stays on the anchor; no propagation).
- **Skill:** update kanban-orchestrator to "decompose via `kanban_decompose`
  (self-park); do **not** complete your task in run #1; aggregate + complete in
  run #2."

## Testing strategy

- **`kanban_decompose` tool:** running-task self-decompose → assert children
  created, root linked as child-of-leaves, root `running → todo`, run #1 ended;
  all-children-done → root re-promotes; run #2 completes.
- **`_wake_origin_session`:** fake `_handle_message` — assert called with the
  right `SessionSource` + handoff; cursor advances on success, falls back to
  direct `msg` on failure; no wake for `direct`/`silent`/non-`completed`;
  artifact-dedup suppression; config-mode resolution + CLI clamp.
- **Blackboard roll-up:** child transitions post to T0; `latest_blackboard`
  merges last-write-wins.
- Local tests under `tests/local/`; remove the inline edit in upstream
  `tests/gateway/test_kanban_notifier.py` (referenced `_synthesize_kanban_notification`).

## PR sequencing (small, independently justifiable)

1. **PR-A — `add_notify_sub` upsert** (our #20). Already blessed by TheoLong.
   Pure correctness, no schema change. Land first.
2. **PR-B — `SendResult(success=False)` → retry.** Uncontroversial.
3. **PR-C — lifecycle hardening + sticky-block** (our #5 / `b2301c4d1`). General
   robustness; zero upstream-test edits. Independent of notifications.
4. **PR-D — `kanban_decompose` tool** (generalize upstream `decompose_triage_task`
   off the triage guard; expose as an orchestrator tool). Independently useful
   (durable fan-in for any orchestrating profile), no gateway changes.
5. **PR-E — wake-origin-session + config mode** (this doc's delivery half). Open
   as an RFC/issue first, citing teknium1's follow-up; frame as "not a third path
   — rendering goes through the normal agent loop," leading with background
   dispatch + failure fallback to pre-empt the concurrency concern.

## Appendix: prototype evidence + key references

- Wake-primitive spike (branch `worktree-kanban-wake-primitive`, uncommitted):
  replaced the synthesize branch in `_send_kanban_notification` with
  `_wake_origin_session`, deleted the synthesis apparatus, removed unused
  imports. `gateway/kanban_synthesis.py` 574 → 266 lines; `py_compile` clean;
  surviving methods exactly `[_send_kanban_notification, _wake_origin_session]`.
  `kanban_decompose` and blackboard roll-up are **not** prototyped yet.
- Code references: `decompose_triage_task` `hermes_cli/kanban_db.py:4042`
  (root-as-child-of-leaves link `:4188`); `_process_handoff`
  `gateway/run.py:4805` (synthetic `internal=True` turn → `_handle_message`);
  auto-subscribe `tools/kanban_tools.py:200`; inheritance to retire
  `tools/kanban_tools.py:260`; swarm blackboard `hermes_cli/kanban_swarm.py:226`;
  dispatcher `max_spawn` `gateway/run.py:5201`.
