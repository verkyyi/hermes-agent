# Local Patches Inventory

This fork carries local patches on top of upstream Hermes Agent (NousResearch).
This document inventories them — what each patch is, why it exists, and where its
test coverage lives — so the divergence stays legible across upstream merges.

- **Running branch:** `verky/deploy`
- **Upstream baseline:** caught up to `upstream/main` @ `2d5dcfabc` via merge `4a9607afd` (2026-05-27). Originally forked at `v2026.5.16` / `a91a57fa5`.
- **Upstream mirror:** local `main` tracks `upstream/main`
- **Last upstream sync:** 2026-05-27 — 1,239 commits. Upstream refactored `run_agent.py` and parts of `gateway/run.py` into new `agent/*` modules, so several patches were re-homed (noted per-patch below). Test infra moved to `pytest-timeout` (xdist dropped).

> **Maintenance workflow.** Keep `main` a pristine upstream mirror — fast-forward
> only (`git fetch upstream && git branch -f main upstream/main`), never merge
> local patches into it. Sync by merging `main` (or an upstream tag) *into*
> `verky/deploy`. Local test additions live under `tests/local/` (mirrors the
> upstream test layout) so upstream merges don't conflict on them — see
> [Test organization](#test-organization). Keep this file current when adding,
> removing, or materially changing a local patch.
>
> **Merge-surface budget.** Run `python scripts/merge_surface.py` to see where the
> fork's *conflict* surface lives — it ranks tracked source files by the
> ``git diff --numstat`` deletions/modifications column (edits to lines upstream
> owns), which is what actually conflicts on a sync; new files and pure additions
> (`+N -0`) are reported separately as low-risk. Watch this across syncs so
> divergence doesn't creep; `--check N` exits non-zero if any source file exceeds
> a per-file modified-line budget (CI gate). Moving patches onto extension points
> (#6 → plugin hook, #14 → `register_cli_command` plugin) is how that number goes
> down. Current hot files: `gateway/run.py` (~622) and `hermes_cli/kanban_db.py`
> (~108).
>
> **Tier-2 conclusion (these two hot files are largely irreducible).** Their
> *conflict* surface (the ~622 / ~108 modified-line counts) is dominated by fork
> changes **woven into upstream control flow**, not separable added blocks:
> `gateway/run.py` — the conversation-lock `async with` wrap of an upstream
> try/finally and the public-progress loop rewrite; `hermes_cli/kanban_db.py` —
> `expected_run_id` scoping added inside existing `UPDATE`s (#5), crash-detection
> fingerprinting interleaved through `detect_crashed_workers`, and the removed
> truncation in `complete_task` (#3, a deletion — nothing to extract). Extracting
> any of these would restructure upstream code *more* (bigger diffs, behavior
> risk in the exact state machine that corrupted `kanban.db` on 2026-05-27), so
> they are deliberately **left in place** — resolve at merge time, guarded by the
> #5/#20 tests. The only safe reduction available was the *added* column of
> `run.py`: three entirely fork-added `GatewayRunner` methods
> (`_conversation_lock_for_key`/`_for_source`, `_handle_metrics_command`) moved
> into `gateway/gateway_forklocal.py::ForkLocalGatewayMixin`
> (`class GatewayRunner(KanbanSynthesisMixin, KanbanNotifierMixin, ForkLocalGatewayMixin)`),
> dropping `run.py`'s added column 657→631 with the bodies now in a zero-conflict
> new file. Behavior-preserving (gateway suite parity; ruff clean). Tier-1 #10
> (AgentFeeds toolset → plugin) was **declined**: it would remove only ~2 conflict
> lines from `toolsets.py` while coupling front-desk AgentFeeds (3 platforms) to
> an opt-in plugin — net-negative.

## Summary

| # | Area | Commit(s) | Test coverage |
|---|------|-----------|---------------|
| 1 | Kanban origin-return + notification modes — **delivery reworked, see #21** (mode now config-resolved; sub mode/origin columns vestigial for delivery) | `be4900d9d`, `0a75a7315` | `tests/gateway/test_kanban_notifier.py`, `tests/tools/test_kanban_tools.py` (+ `tests/local/`) |
| 2 | Kanban completion delivery in `KanbanSynthesisMixin` — **gateway-side LLM synthesis removed, replaced by origin-session wake (#21)** | `39a5bf9ff` | `tests/local/gateway/test_kanban_notifier.py` |
| 3 | Full handoff summary in the completed event (no first-line/400-char cap) | `39a5bf9ff`, `3592c3510` | `tests/hermes_cli/test_kanban_core_functionality.py` |
| 4 | Fork-local notify-sub schema guards (idempotent ALTERs for the 5 origin/mode columns) | `3592c3510`, `7ee088258` | `tests/tools/test_kanban_tools.py` |
| 5 | Kanban lifecycle recovery hardening (heartbeat/claim/stale-run/audit) | `380eec386` | `tests/hermes_cli/test_kanban_db.py` (+ `tests/local/`) |
| 6 | Orchestrator 3-layer routing (front-desk → orchestrator → workers) | `95757a2c3`; routing guard moved to plugin `kanban-orchestrator-routing` | `tests/orchestrator_benchmark/test_frontdesk_routing.py` (10) |
| 7 | Orchestrator hardening benchmark (executable TDD spec) | `13c1fc21e` | `tests/orchestrator_benchmark/` + `evals/orchestrator_routing/` (GREEN guards + `xfail(strict)` TDD targets) |
| 8 | Replay wall-clock timestamp prefix on user messages | `39a5bf9ff` | `tests/gateway/test_user_timestamp_prefix.py` (5) |
| 9 | AgentFeeds stable manifest in the cache-stable prompt region | `2ea24d4bb`, `62aa2c45c` | `tests/local/run_agent/test_agentfeeds.py` |
| 10 | Read-only AgentFeeds toolset (`agentfeeds_read`/`agentfeeds_search`) | `0a75a7315` | `tests/tools/test_agentfeeds_readonly_tool.py` |
| 11 | Front-desk UX slimming + explicit-skill policy | `0a75a7315` | `tests/gateway/test_config.py`, skills tests |
| 12 | Telegram pre-LLM acknowledgement (front-desk responsiveness) | `0a75a7315` | `tests/gateway/test_telegram_pre_llm_ack.py` (+ benchmark #16) |
| 13 | Segmented agent telemetry (`agent/telemetry.py`) | `0a75a7315` | `tests/test_telemetry.py` |
| 14 | Break-glass operator escape hatch (`hermes_cli/break_glass.py`) | `0a75a7315`; CLI wiring moved to plugin `break-glass-cli` | `tests/hermes_cli/test_break_glass.py`, `tests/local/hermes_cli/test_break_glass_cli_plugin.py` |
| 15 | Google Workspace OAuth setup hardening | `0a75a7315` | `tests/local/skills/test_google_oauth_setup.py` |
| 16 | Profile-memory dashboard plugin | `ee0334194` | `tests/plugins/test_profile_memory_dashboard_plugin.py` |
| 17 | Default-profile responsiveness benchmark + live TTFT | `95772b8f5` | `tests/responsiveness_benchmark/` (28 GREEN + 2 `xfail`) |
| 18 | Re-applied CVE security pins dropped by the v2026.5.16 merge | `84ceb225c` | _none dedicated_ (lockfile/pin change) |
| 19 | `tests/local/` extraction (merge-pain reduction) | `894daa376` | _test-organization meta_ |
| 20 | Notify-subscription upsert (re-subscribe updates mode/origin; no cursor reset) | `c653c8881` | `tests/local/hermes_cli/test_kanban_db.py` |
| 21 | Decompose-anchor + wake-origin-session delivery redesign (supersedes the synthesis half of #1/#2) | `417e21530`, `8964a887a`, `baf695a16`, `b60229dec` | `tests/local/gateway/test_kanban_notifier.py`, `tests/local/hermes_cli/test_kanban_decompose_selfpark_db.py`, `tests/local/tools/test_kanban_decompose_tool.py`, `tests/local/agent/test_kanban_orchestrator_guidance.py` |

---

## Kanban delegation

### 1. Origin-return + notification modes (`be4900d9d`, parts of `0a75a7315`)
Agent-created Kanban tasks record an **origin notification subscription** so a
worker's completion can return to the surface that requested the work (Telegram
chat/thread, Weixin, CLI session). Three modes:
- `direct` — post the raw completion to the origin.
- `synthesize` — the origin/default profile rewrites the worker handoff into a
  normal user-facing reply (interactive surfaces).
- `silent` — internal child/fan-out tasks stay quiet.

Successful deliveries are mirrored into the origin session context; on a failed
platform send the subscription **retries** instead of silently dropping.
Parentless worker-created recovery/root tasks inherit the original subscription;
ordinary children stay quiet by default.
Key files: `gateway/run.py`, `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`,
`model_tools.py`, `toolsets.py`. Post-merge, the `request_id` plumbing lives in
`agent/tool_executor.py` and `agent/agent_runtime_helpers.py` (upstream's refactor).

> **Refactor — done (`4c3843555`).**
> Most of this patch is additive (new `kanban_notify_subs` table + functions in
> `kanban_db.py`, new tool surface in `kanban_tools.py`) and rarely conflicts on
> merge. The real merge pain was the notifier in the hot upstream file
> `gateway/run.py`. The terminal-event notifier — `_kanban_notifier_watcher()`
> plus `_kanban_notify_in_gateway_enabled`, `_active_profile_name`,
> `_kanban_advance`, `_kanban_unsub`, `_kanban_rewind`,
> `_deliver_kanban_artifacts` — was lifted into
> `gateway/kanban_notifier.py::KanbanNotifierMixin`, mixed in via
> `class GatewayRunner(KanbanSynthesisMixin, KanbanNotifierMixin)` (extending the
> #2 mixin pattern). Methods moved byte-for-byte; the watcher `create_task(...)`
> and the module-level progress/notify constants + helpers
> (`_KANBAN_NOTIFY_KINDS`, `_public_progress_interval_from_env`,
> `_kanban_heartbeat_progress_message`) stay in `run.py` and are imported lazily
> in the watcher to avoid a circular import. The watcher keeps its privileged
> `self.*` access (adapters, `_send_kanban_notification`, per-sub state).
> `run.py` 19,392 → 18,796 lines (−610). Behavior-preserving: notifier+kanban
> suite 484 passed / 1 skipped, ruff clean, ty neutral for `run.py`.
> **A plugin/hook rewrite was considered and rejected:** the feature is a
> cross-process background reactor (completion fires in a separate worker process;
> the gateway polls DB state on a ~5s tick) that needs gateway internals the
> plugin contract (`hermes_cli/plugins.py`) and gateway hooks (`gateway/hooks.py`)
> deliberately don't expose. A plugin would first require new core extension
> points (background-task registration, a `kanban_event` hook, adapter/mirror
> access) — that's *more* local divergence, not less, unless those points are
> upstreamed.

### 2. Completion synthesis → `KanbanSynthesisMixin` (`39a5bf9ff`)
The `synthesize`-mode logic (origin-profile LLM rewrite, sanitized timeout
fallback, bounded artifact-excerpt loading) was extracted out of `GatewayRunner`
into `gateway/kanban_synthesis.py::KanbanSynthesisMixin`
(`class GatewayRunner(KanbanSynthesisMixin)`) to shrink the merge surface of the
single largest local patch against upstream `gateway/run.py`. Behavior-preserving.

### 3. Full handoff summary (`39a5bf9ff`, `3592c3510`)
`complete_task` no longer slices the completed-event payload summary to the first
line / 400 chars, and the gateway notifier no longer truncates the rendered
handoff. The Kanban DB is the single source of truth; downstream readers
(Telegram, synthesis, dashboard WS) must see the complete handoff.

### 4. Fork-local notify-sub schema guards (`3592c3510`, corrected in `7ee088258`)
`_migrate_add_optional_columns` keeps idempotent `ALTER TABLE … ADD COLUMN`
guards for `notification_mode` / `origin_session_id` / `origin_profile` /
`origin_context` / `request_id` (plus the longstanding `notifier_profile`).

> **Correction.** `3592c3510` originally *removed* these five guards on the
> stated rationale that the columns were "in the base `CREATE TABLE` schema as
> of upstream v0.14.0." **That premise is false.** These columns are
> **fork-local**: they exist in the upstream tree only on the **unmerged** PR
> branch `upstream/pr-21523` (`be4900d9d` is *our* commit / the PR head, not an
> upstream merge). Even after the 2026-05-27 catchup, upstream `main`
> (`origin/main` @ `2d5dcfabc`) ships `kanban_notify_subs` with **only**
> `notifier_profile` — verified zero of the five columns present. Because
> they're part of *our* base `CREATE TABLE` but not upstream's, and
> `CREATE TABLE IF NOT EXISTS` is a no-op against a table first created by an
> upstream-schema (or older-fork) checkout, the ALTER guards are the **only**
> mechanism that backfills the columns on such a DB. They were restored in
> `7ee088258`. **Preserve these across upstream merges** (upstream won't supply
> them); they can be retired only once PR #21523 actually lands upstream — add
> to the #18 per-merge re-verification list.

### 5. Lifecycle recovery hardening (`380eec386`)
Hardens kanban run lifecycle: heartbeat extends only the owner's current run,
stale heartbeats don't extend foreign runs, completion-rejection context
identifies stale runs, blocked-task recovery preserves audit history, and
spawn-failure payloads include a log tail.
Key file: `hermes_cli/kanban_db.py`.

> **Permanent-failure sticky-block (`b2301c4d1`).** A preflight skill failure
> (`_preflight_task_skills` → "Unknown skill(s): X" — a missing/disabled *forced*
> skill) is **permanent**: it can never succeed on retry. The circuit breaker now
> emits a **sticky `blocked`** event (via a `permanent=True` flag threaded through
> `_record_spawn_failure` → `_record_task_failure`) instead of the auto-recoverable
> `gave_up`, so `recompute_ready` / `_has_sticky_block` park the task for a human
> instead of respawning it every dispatcher tick forever — the loop that helped
> corrupt `kanban.db` on 2026-05-27 (~2.4 spawn/s). **Scoped to the preflight call
> site only:** transient failures (crash/timeout) still emit `gave_up` and keep
> upstream's auto-recovery, so upstream's tested breaker contract
> (`test_kanban_blocked_sticky.py`; the `gave_up` assertions in upstream
> `test_kanban_core_functionality.py`) is untouched — **zero upstream test edits.**
> Recovery: fix the skill/profile + `hermes kanban unblock`, or `archive`.
> Test: `tests/local/hermes_cli/test_kanban_core_functionality.py::test_dispatch_preflight_unknown_forced_skill_blocks_without_spawn`.
> **Deferred:** bounding *transient*-never-clears (a non-permanent failure that
> keeps recurring) still relies on upstream's per-streak `failure_limit`; a lifetime
> cap + backoff (Layer 2) was scoped out to avoid diverging from upstream's
> auto-recover contract.

### 20. Notify-subscription upsert (`c653c8881`)
`add_notify_sub` uses `INSERT ... ON CONFLICT(task_id,platform,chat_id,thread_id)
DO UPDATE` instead of `INSERT OR IGNORE`: a re-subscribe updates
`notification_mode` and back-fills origin/identity fields (COALESCE-preserving
any the new call omits), never duplicates the row, and never resets the
delivery cursor (`last_event_id`) or `created_at`. Ported from the retired
`agent-driven-kanban-orchestration` branch (task t_cd8321e9); that branch's
other change (per-origin conversation locks) was already in deploy.
Key file: `hermes_cli/kanban_db.py`.

### 21. Decompose-anchor + wake-origin-session delivery (`417e21530`, `8964a887a`, `baf695a16`, `b60229dec`)
Reworks the kanban completion-delivery path designed in
`docs/plans/2026-05-28-kanban-wake-origin-session.md` (the response to upstream
PR #21523 being closed). Two primitives, both on existing machinery:

- **`kanban_decompose` self-park (Path A).** Generalizes upstream
  `decompose_triage_task` with an opt-in `allow_running` flag so an orchestrator
  can fan out *its own* in-flight task and park it as the fan-in **anchor**
  (`running → todo`, run ended via `_end_run(outcome="decomposed")`, task-level
  claim cleared so the clean worker exit isn't a protocol violation). The anchor
  re-promotes and re-dispatches the orchestrator once all children finish, to
  judge/aggregate. Exposed as the `kanban_decompose` model tool; the
  kanban-orchestrator skill + `KANBAN_GUIDANCE` now prefer it over
  create-then-complete. Key files: `hermes_cli/kanban_db.py`,
  `tools/kanban_tools.py`, `toolsets.py`, `agent/prompt_builder.py`,
  `skills/devops/kanban-orchestrator/SKILL.md`.
- **Wake-origin-session delivery.** Replaces the gateway-side LLM synthesis
  (the whole apparatus deleted from `gateway/kanban_synthesis.py`, 574→~280
  lines) with `_wake_origin_session`: a `synthesize`-mode completion re-enters
  the worker handoff into the origin gateway session as a synthetic
  `internal=True` turn via `_handle_message` (the proven `_process_handoff`
  pattern), so the origin profile composes/delivers the reply through the normal
  agent loop — no second rendering engine. `_wake_with_fallback` bounds the wake
  by `kanban.notify.wake_timeout_seconds` and falls back to the direct status
  line on error/timeout. Key files: `gateway/kanban_synthesis.py`,
  `gateway/kanban_notifier.py`.

**Delivery mode is now operator policy, not a per-task column.**
`_resolve_kanban_notify_mode` reads `HERMES_KANBAN_NOTIFY_MODE` env >
`kanban.notify.<platform>.mode` > `kanban.notify.mode` > a built-in default that
preserves the prior Telegram→`synthesize` default (so deploy does **not** regress
without config). The five `kanban_notify_subs` origin/mode columns (#1/#4) are
now **vestigial for delivery** — kept in-schema for back-compat, no longer read
to decide delivery, removing the footgun of a model setting `silent` on a
user-visible task. The notifier also skips watcher artifact delivery in
`synthesize` mode (the woken agent surfaces artifacts itself).

**Origin-return reliability hardening (`3849a3663`, `e38ecbf47`, `33e6526cf`).**
A real-LLM e2e (`evals/origin_return/run.py`, run *outside* pytest — the suite
isolates `HERMES_HOME` and `hermes_cli/auth.py` blocks real creds under
`PYTEST_CURRENT_TEST`) drove the front-desk→orchestrator→worker flow and found
three issues, now fixed:
- **Wake self-deadlock (`3849a3663`):** `_send_kanban_notification` wrapped the
  wake in the per-session conversation lock that the wake's own `_handle_message`
  re-acquires → 180s timeout → silent degrade to a direct send. Fixed by running
  the wake outside that lock (it self-serializes).
- **Origin sub stranded on a router (`e38ecbf47`):** when a task carrying an
  origin sub completes with pending children it delegated to, the sub now moves
  onto those children (completion-time safety net; leaf/done-children cases keep
  it).
- **Reliable self-park (`33e6526cf`):** guidance alone wouldn't stop the
  orchestrator from create+completing, so `kanban_complete` now intercepts a
  return-anchor with pending delegated children and `park_as_fanin_anchor`s it
  (reverse links → children run, anchor waits; park at `todo`; keep the sub).
  The anchor stays the single return point and re-wakes to aggregate + deliver,
  whether the orchestrator used `kanban_decompose` or create+complete.
Tests: `tests/local/hermes_cli/test_origin_sub_propagation.py` (4),
`tests/local/hermes_cli/test_self_park_enforce.py` (5), and the gated real-LLM
e2e under `evals/origin_return/` (phase a + b green).

> **Verification.** Deterministic paths are unit-covered; the front-desk
> origin-subscribe and orchestrator self-park are now also covered by the gated
> real-LLM e2e (`HERMES_RUN_LLM_EVALS`-style, run outside pytest). Still worth a
> one-task Telegram smoke-test after deploy for the full live
> `park → re-dispatch → aggregate → wake → deliver` loop.
> **Upstream-PR prep (deferred):** dropping the vestigial columns (schema
> collapse to upstream-identical) and Path-B (front-desk aggregation) remain
> future work per the design doc.

### 6 & 7. Orchestrator 3-layer routing + benchmark (`95757a2c3`, `13c1fc21e`)
**Routing guard:** a `create` from the default front-desk profile must use
`assignee='orchestrator'` (reject-only `tool_error`, no coercion). The
orchestrator and workers are exempt — fan-out to worker lanes is their job.
Key file: `tools/kanban_tools.py`.
**Benchmark:** an executable spec for the 3-layer design (default → orchestrator
→ workers → fan-in) under `tests/orchestrator_benchmark/` + `evals/
orchestrator_routing/`: linking/ownership, block grouped-notify, transient
auto-recovery, garbage auto-archive, e2e lifecycle, LLM routing eval. GREEN tests
guard current behavior; **`xfail(strict)` targets** flip to failures once their
`kanban_db` contract functions are implemented (pending feature work).

> **Routing guard → plugin (`plugins/kanban-orchestrator-routing/`).** The
> reject-only `create` guard was lifted out of an inline edit in the hot upstream
> file `tools/kanban_tools.py::_handle_create` into an opt-in standalone plugin
> that registers a **`pre_tool_call`** hook. The hook returns
> `{"action": "block", "message": ...}` for a front-desk `kanban_create` aimed at
> a non-orchestrator lane; the executor wraps it as `{"error": <message>}` — the
> same shape the old inline `tool_error` produced. Scope/exemptions are unchanged
> (it reads the same `HERMES_PROFILE` / `HERMES_KANBAN_TASK` env vars the inline
> code read). `_handle_create` is now routing-agnostic; the only remaining core
> touch is upstream's own `pre_tool_call` dispatch in `agent/tool_executor.py`.
> **Tradeoff — opt-in:** standalone plugins load only when listed in
> `plugins.enabled`, so this safety invariant is now config-gated rather than
> always-on. The deploy config must enable `kanban-orchestrator-routing` for the
> guard to be active; `KANBAN_ORCHESTRATOR_ROUTING_DISABLE=1` keeps it installed
> but inert. Tests (`test_frontdesk_routing.py`) cover the hook scope, the
> guardrails (absent assignee not masked, non-create tools ignored, disable flag),
> the now-routing-agnostic handler, and the real plugin-manager wiring **and**
> opt-in gating end-to-end. Verified: 10 passed; the full
> `tests/tools/test_kanban_tools.py` + `tests/orchestrator_benchmark/` suite
> stays green (179 passed / 1 skipped / 20 xfailed).

---

## Front-desk experience

### 8. Replay timestamp prefix (`39a5bf9ff`)
`gateway/run._format_user_timestamp_prefix` prepends `[YYYY-MM-DD HH:MM TZ]`
(America/Los_Angeles) to replayed **user** messages so the model perceives
send-time and inter-turn gaps. Applied at replay only (not persisted → no
double-prefix); plain-string user content only, multimodal parts untouched.
**Home (post-merge):** applied in `gateway/run.py::_build_gateway_agent_history`.

### 11. UX slimming + explicit-skill policy (`0a75a7315`)
Hides internal Kanban plumbing from normal Telegram/Weixin replies by default
(task ids, worker names, dispatcher details, run ids, subscription state) unless
debug detail is requested. Adds focused front-desk skill allowlists + tool
slimming for messaging surfaces while preserving broad CLI capability.
Explicit/preloaded task skills bypass ambient `skills.allowed` /
`skills.platform_allowed` filters (so Kanban work can force-load needed skills on
lean workers), with hard guardrails preserved via `skills.disabled` /
`skills.platform_disabled` / `skills.forced_denied` / `skills.platform_forced_denied`.

### 12. Telegram pre-LLM ack (`0a75a7315`)
Sends an immediate front-desk acknowledgement before the LLM turn so users get
fast feedback on long-running work. See also the responsiveness benchmark (#17).

---

## Context, tools, observability

### 9. AgentFeeds stable manifest (`2ea24d4bb`, `62aa2c45c`)
The AgentFeeds system manifest (a stable, per-session stream inventory that
excludes volatile freshness/health/content) is appended to `stable_parts` so it
lands in the cache-stable prompt prefix rather than the volatile tail.
**Homes (post-merge):** helper functions stay in `run_agent.py`; the wiring is in
`agent/system_prompt.py::build_system_prompt_parts` (lazy `import run_agent`,
monkeypatch-safe); config init in `agent/agent_init.py`.
NOTE: the pre-patch code appended to a non-existent `prompt_parts` local — a
latent `NameError` when the manifest was enabled — so this both fixes the crash
and improves prompt caching.

### 10. Read-only AgentFeeds toolset (`0a75a7315`)
A narrow `agentfeeds_readonly` toolset (`agentfeeds_read`, `agentfeeds_search`)
over cached AgentFeeds state. Does **not** refresh feeds, subscribe/unsubscribe,
run commands, fetch the web, or read arbitrary files — gives front-desk flows
compact AgentFeeds context without the mutating AgentFeeds/terminal/file surface.
Key file: `tools/agentfeeds_readonly_tool.py`.

### 13. Segmented telemetry (`0a75a7315`)
`agent/telemetry.py` — segmented dispatch/completion telemetry; surfaces
ttft/ttfa/ttlt used by the responsiveness benchmark's live mode.

### 14. Break-glass (`0a75a7315`)
`hermes_cli/break_glass.py` — operator escape hatch for recovering stuck
state.

> **CLI wiring → plugin (`plugins/break-glass-cli/`).** The `hermes break-glass`
> subcommand was wired by an inline edit in `hermes_cli/main.py` (an `import` +
> `build_parser(subparsers)` call). That moved into a `break-glass-cli` plugin
> via `register_cli_command` — `main.py` is now **upstream-identical** (was
> +7/−0). `break_glass.py` was refactored to expose `configure_parser(parser)`
> (populates an already-created parser — the plugin `setup_fn` contract) plus
> `BREAK_GLASS_HELP`/`BREAK_GLASS_DESCRIPTION` constants; `build_parser` stays as
> a back-compat shim. **Tradeoff — opt-in + swallowed discovery:** plugin CLI
> commands load only when listed in `plugins.enabled`, and registration runs
> inside `discover_plugins()` whose failures the CLI swallows. Since break-glass
> is an emergency tool, **keep `break-glass-cli` enabled** (it is, in deploy
> config) so the subcommand can't silently vanish when the runtime is degraded.
> Verified live: `hermes break-glass --help` lists all five actions sourced from
> the plugin. Tests: `tests/local/hermes_cli/test_break_glass_cli_plugin.py`
> (parser dispatch, register wiring, real-manager wiring + opt-in gating).

### 15. Google Workspace OAuth setup (`0a75a7315`)
Hardening of `skills/productivity/google-workspace/scripts/setup.py` (scope
filtering, JSON auth-url payloads, fresh-auth-url-on-failure).

### 16. Profile-memory dashboard plugin (`ee0334194`)
`plugins/profile-memory/dashboard/` — dashboard plugin for editing profile
memory (plugin_api + bundled UI).

### 17. Responsiveness benchmark + live TTFT (`95772b8f5`)
Deterministic benchmark over emulated user sessions for the front-desk default
profile — drives the real gateway pre-LLM-ack + public-progress policies and
scores time-to-first-feedback. Opt-in live TTFT mode (`run_live.py`) invokes the
real agent and reads measured ttft/ttfa/ttlt from telemetry. Ships a Claude Code
skill (`.claude/skills/responsiveness-benchmark/SKILL.md`) — the **only** file
this fork tracks under `.claude/` (no `.gitignore` rule covers `.claude/`, so it
was committed deliberately). Relocate or drop it if upstreaming; a personal copy
at `~/.claude/skills/responsiveness-benchmark/` serves non-repo sessions either way.
Files: `evals/responsiveness/`, `tests/responsiveness_benchmark/`,
`.claude/skills/responsiveness-benchmark/SKILL.md`.

---

## Security & maintenance

### 18. CVE security re-pins (`84ceb225c`)
The `v2026.5.16` tag merge reverted `pyproject.toml`'s aiohttp/anthropic pins and
dropped the explicit `cryptography` floor (while `uv.lock`/`lazy_deps.py` stayed
secure — an inconsistent HEAD). Restored: `aiohttp==3.13.4` (CVE-2026-34513/
34518/34519/34520/34525), `anthropic==0.87.0` (CVE-2026-34450/34452),
`cryptography==46.0.7` (CVE-2026-39892). Re-check these on every upstream merge.

### 19. tests/local/ extraction (`894daa376`)
See [Test organization](#test-organization).

---

## Test organization

To keep upstream merges conflict-free, **local-only test additions live under
`tests/local/`**, which mirrors the upstream test directory layout. The official
upstream test files therefore stay byte-identical to upstream except for a small
set of irreducible **inline edits** to existing upstream tests/fixtures that
encode local behavior changes (the full-summary assertion, `request_id` kwargs,
env-isolation `delenv` loops, and the two `restart_*` files which are pure inline
edits). This cut the test-file merge-conflict surface from ~1,660 to ~133 lines.

`tests/local/conftest.py` re-exposes the directory-scoped fixtures the extracted
tests rely on (telegram/discord `sys.modules` mocks from the gateway conftest,
`all_assignees_spawnable` from hermes_cli, the run_agent autouse retry-backoff
short-circuit). Extracted tests import upstream helpers/fixtures from their
original modules; the `tests/local/skills/` file is self-contained because
`tests/skills` is not a package.

**When adding new local tests, put them under `tests/local/<mirror-path>/` rather
than editing upstream test files.**
