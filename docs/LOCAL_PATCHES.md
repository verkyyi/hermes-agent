# Local Patches Inventory

This fork carries local patches on top of upstream Hermes Agent (NousResearch).
This document inventories the patches **as they stand now** — what each is, why
it exists, where its tests live, and any flag that must survive an upstream
merge — so the divergence stays legible. (Entry numbers are stable IDs reused in
commit messages; they are not renumbered when content is consolidated.)

- **Running branch:** `verky/deploy` · **Upstream mirror:** local `main` tracks `upstream/main`
- **Upstream baseline:** `upstream/main` @ `2d5dcfabc` via merge `4a9607afd` (2026-05-27). Forked at `v2026.5.16` / `a91a57fa5`.
- **Last upstream sync:** 2026-05-27 (1,239 commits). Upstream refactored `run_agent.py` / `gateway/run.py` into `agent/*` modules; re-homed patches are noted per entry. Test infra moved to `pytest-timeout` (xdist dropped).

> **Workflow.** Keep `main` a pristine upstream mirror — fast-forward only
> (`git fetch upstream && git branch -f main upstream/main`), never merge local
> patches into it. Sync by merging `main` (or an upstream tag) *into*
> `verky/deploy`; never rebase. Local-only tests live under `tests/local/`
> (mirrors the upstream layout) so syncs don't conflict on them. Keep this file
> current when adding, removing, or materially changing a patch.
>
> **Merge-surface budget.** `python scripts/merge_surface.py` ranks tracked files
> by *conflict* surface (the `git diff --numstat` modified/deleted column — edits
> to lines upstream owns); pure-additive new files are reported separately as
> low-risk. `--check N` is a CI gate (non-zero if any file exceeds a per-file
> modified-line budget). Hot files: `gateway/run.py` (~631) and
> `hermes_cli/kanban_db.py` (~108). Both are **largely irreducible** — their
> surface is fork logic woven into upstream control flow (the conversation-lock
> `async with` wrap + public-progress loop in `run.py`; `expected_run_id` scoping
> and crash-fingerprinting interleaved through `kanban_db.py`), so they are left
> in place and resolved at merge time, guarded by the #5/#20/#21 tests. The
> reducible part — entirely fork-added methods — has been lifted into
> zero-conflict mixin files (see #1/#2/#21).

## Summary

| # | Area | Test coverage |
|---|------|---------------|
| 1 | Kanban origin-return + notification subscription — **folded into #21** | see #21 |
| 2 | Completion-delivery mixin extraction — **folded into #21** | see #21 |
| 3 | Full handoff summary in the completed event (no first-line/400-char cap) | `tests/hermes_cli/test_kanban_core_functionality.py` |
| 4 | Fork-local notify-sub schema guards (idempotent ALTERs) — **preserve across merges** | `tests/tools/test_kanban_tools.py` |
| 5 | Kanban lifecycle recovery hardening + permanent-failure sticky-block | `tests/hermes_cli/test_kanban_db.py` (+ `tests/local/`) |
| 6 | Orchestrator routing guard (front-desk → orchestrator) — plugin `kanban-orchestrator-routing` | `tests/orchestrator_benchmark/test_frontdesk_routing.py` |
| 7 | Orchestrator hardening benchmark (executable TDD spec) | `tests/orchestrator_benchmark/` + `evals/orchestrator_routing/` |
| 8 | Replay wall-clock timestamp prefix on user messages | `tests/gateway/test_user_timestamp_prefix.py` |
| 9 | AgentFeeds stable manifest in the cache-stable prompt region | `tests/local/run_agent/test_agentfeeds.py` |
| 10 | Read-only AgentFeeds toolset (`agentfeeds_read`/`agentfeeds_search`) | `tests/tools/test_agentfeeds_readonly_tool.py` |
| 11 | Front-desk UX slimming + explicit-skill policy | `tests/gateway/test_config.py`, skills tests |
| 12 | Telegram pre-LLM acknowledgement | `tests/gateway/test_telegram_pre_llm_ack.py` |
| 13 | Segmented agent telemetry (`agent/telemetry.py`) | `tests/test_telemetry.py` |
| 14 | Break-glass operator escape hatch — CLI via plugin `break-glass-cli` | `tests/hermes_cli/test_break_glass.py`, `tests/local/hermes_cli/test_break_glass_cli_plugin.py` |
| 15 | Google Workspace OAuth setup hardening | `tests/local/skills/test_google_oauth_setup.py` |
| 16 | Profile-memory dashboard plugin | `tests/plugins/test_profile_memory_dashboard_plugin.py` |
| 17 | Default-profile responsiveness benchmark + live TTFT | `tests/responsiveness_benchmark/` |
| 18 | Re-applied CVE security pins — **re-check every merge** | _none dedicated (lockfile/pin)_ |
| 19 | `tests/local/` extraction (merge-pain reduction) | _test-organization meta_ |
| 20 | Notify-subscription upsert (re-subscribe updates mode/origin; no cursor reset) | `tests/local/hermes_cli/test_kanban_db.py` |
| 21 | **Kanban completion delivery & origin-return** (the consolidated design — supersedes #1/#2) | `tests/local/gateway/test_kanban_notifier.py`, `tests/local/hermes_cli/test_kanban_decompose_selfpark_db.py`, `tests/local/hermes_cli/test_origin_sub_propagation.py`, `tests/local/hermes_cli/test_self_park_enforce.py`, `tests/local/tools/test_kanban_decompose_tool.py` |

---

## Kanban delegation & delivery

### 21. Kanban completion delivery & origin-return (consolidated)

The whole completion-delivery design — how a background worker's result returns
to the surface that asked for it. Designed in
`docs/plans/2026-05-28-kanban-wake-origin-session.md` (the response to upstream
PR #21523 being closed). This supersedes the original synthesis path; #1 and #2
are its lineage.

**Subscription & routing.** An agent-created task records an origin notification
subscription (Telegram chat/thread, Weixin, CLI). One tracking task = one
subscription = one return. When a task carrying a sub completes with pending
children it delegated to, the sub propagates onto those children (`e38ecbf47`),
so the answer is never stranded on a router. `add_notify_sub` upserts on
`(task,platform,chat,thread)` — a re-subscribe updates the mode and COALESCE-
backfills identity fields, never duplicating the row or resetting the delivery
cursor (#20, `c653c8881`).

**Delivery mode is operator policy, not a per-task column.**
`_resolve_kanban_notify_mode` reads env `HERMES_KANBAN_NOTIFY_MODE` >
`kanban.notify.<platform>.mode` > `kanban.notify.mode` > built-in default
(Telegram → `synthesize`, else `direct`). Modes: `direct` (post the raw
handoff), `synthesize` (wake the origin session — below), `silent` (internal
fan-out children). The five fork-local origin/mode columns (#1/#4) are now
**vestigial for delivery** — kept in-schema for back-compat but no longer read
to decide delivery, which removes the footgun of a model setting `silent` on a
user-visible task.

**Wake-origin-session (`synthesize`).** Instead of a gateway-side LLM rewrite
(the old #2 apparatus, ~310 lines, deleted), a `completed` event re-enters the
worker handoff into the origin gateway session as a synthetic `internal=True`
turn via `_handle_message` (the proven `_process_handoff` pattern) — the origin
profile composes and delivers the reply through the normal agent loop, with full
tool access; no second rendering engine. The wake runs **outside** the
per-session conversation lock (`_handle_message` acquires it itself; wrapping it
self-deadlocked — `3849a3663`).

**Non-blocking dispatch (`12ddd3bd2`).** The wake runs a full front-desk agent
turn, so awaiting it inline blocked the ~5s notifier-watcher tick — every other
board/subscription/heartbeat delivery stalled behind one chat's turn.
`_send_kanban_notification` now dispatches the wake as a tracked background task
(`_background_tasks`) and returns immediately. `_run_kanban_wake_delivery` owns
the outcome the watcher can no longer observe synchronously: on success it resets
the failure counter and unsubscribes a terminal task; on total failure (wake
*and* the direct-line fallback both fail) it rewinds the claim cursor for retry
or drops a dead subscription after `MAX_SEND_FAILURES`. The watcher defers its
synchronous unsub/fail-reset for the wake case so a failed wake still retries
against the live sub; dedup is safe because the cursor is advanced at claim time.
`_wake_with_fallback` bounds the wake by `kanban.notify.wake_timeout_seconds` and
falls back to the direct status line. Artifact upload is skipped in `synthesize`
mode (the woken agent surfaces artifacts itself — no double-upload).

**Decompose-anchor self-park (`417e21530`, `8964a887a`, `baf695a16`,
`b60229dec`).** `kanban_decompose` generalizes upstream `decompose_triage_task`
with an `allow_running` flag so an orchestrator can fan out *its own* in-flight
task and park it as the fan-in **anchor** (`running → todo`, run ended via
`_end_run(outcome="decomposed")`, task-level claim cleared so the clean exit
isn't flagged as a crash). The anchor re-promotes and re-dispatches the
orchestrator once all children finish, to judge/aggregate. Because guidance alone
won't stop a create-then-complete, `kanban_complete` intercepts a return-anchor
with pending delegated children and parks it as a fan-in anchor (`33e6526cf`) —
the anchor stays the single return point either way. The kanban-orchestrator
skill + `KANBAN_GUIDANCE` prefer `kanban_decompose`.

**Merge-surface homes.** The notifier (`_kanban_notifier_watcher` et al.) lives
in `gateway/kanban_notifier.py::KanbanNotifierMixin`; the delivery/wake half in
`gateway/kanban_synthesis.py::KanbanSynthesisMixin`; fork-added `GatewayRunner`
methods (conversation locks, metrics) in
`gateway/gateway_forklocal.py::ForkLocalGatewayMixin` —
`class GatewayRunner(KanbanSynthesisMixin, KanbanNotifierMixin, ForkLocalGatewayMixin)`.
These are byte-movable extractions that keep the hot `gateway/run.py` smaller.
Key files also: `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`,
`toolsets.py`, `agent/prompt_builder.py`,
`skills/devops/kanban-orchestrator/SKILL.md`.

> **A plugin/hook rewrite was rejected:** this is a cross-process background
> reactor needing gateway internals (background-task registration, adapter/mirror
> access, a `kanban_event` hook) the plugin contract doesn't expose — a plugin
> would add *more* divergence until those points are upstreamed.
>
> **Verification.** Deterministic paths are unit-covered; the front-desk
> origin-subscribe + orchestrator self-park are also covered by a gated real-LLM
> e2e (`evals/origin_return/run.py`, run *outside* pytest — `hermes_cli/auth.py`
> blocks real creds under `PYTEST_CURRENT_TEST`). Worth a one-task Telegram
> smoke-test after deploy for the full live `park → re-dispatch → aggregate →
> wake → deliver` loop.
>
> **Upstream-PR prep (deferred):** dropping the vestigial columns (schema
> collapse to upstream-identical) and Path-B (front-desk aggregation) remain
> future work per the design doc.

### 3. Full handoff summary (`39a5bf9ff`, `3592c3510`)
`complete_task` no longer slices the completed-event summary to the first line /
400 chars, and the notifier no longer truncates the rendered handoff. The Kanban
DB is the single source of truth; downstream readers (Telegram, wake, dashboard
WS) must see the complete handoff. (A deletion in upstream code — nothing to
extract, resolve at merge.)

### 4. Notify-sub schema guards (`3592c3510`, corrected in `7ee088258`) — **preserve**
`_migrate_add_optional_columns` keeps idempotent `ALTER TABLE … ADD COLUMN`
guards for the five fork-local columns (`notification_mode`, `origin_session_id`,
`origin_profile`, `origin_context`, `request_id`) plus `notifier_profile`.

> These columns are **fork-local** — upstream `main` (`2d5dcfabc`) ships
> `kanban_notify_subs` with only `notifier_profile` (they exist upstream only on
> the unmerged PR branch `pr-21523`). `CREATE TABLE IF NOT EXISTS` is a no-op on a
> DB first created by an upstream/older-fork checkout, so the ALTER guards are the
> **only** mechanism that backfills them. **Preserve across every upstream
> merge** (upstream won't supply them); retire only once PR #21523 lands — on the
> #18 per-merge re-verification list.

### 5. Lifecycle recovery hardening (`380eec386`)
Hardens kanban run lifecycle: heartbeats extend only the owner's current run,
stale heartbeats don't extend foreign runs, completion-rejection context
identifies stale runs, blocked-task recovery preserves audit history, and
spawn-failure payloads include a log tail. Key file: `hermes_cli/kanban_db.py`.

> **Permanent-failure sticky-block (`b2301c4d1`).** A preflight skill failure
> (missing/disabled *forced* skill) can never succeed on retry, so the circuit
> breaker emits a sticky `blocked` (via `permanent=True` threaded through
> `_record_spawn_failure` → `_record_task_failure`) instead of auto-recoverable
> `gave_up` — `recompute_ready` / `_has_sticky_block` park it for a human instead
> of respawning every dispatcher tick (the loop that helped corrupt `kanban.db` on
> 2026-05-27 at ~2.4 spawn/s). Scoped to the preflight call site only: transient
> crash/timeout still emit `gave_up` and keep upstream's auto-recovery — **zero
> upstream test edits.** Recovery: fix the skill/profile + `hermes kanban
> unblock`, or `archive`. Test:
> `test_kanban_core_functionality.py::test_dispatch_preflight_unknown_forced_skill_blocks_without_spawn`.

### 6 & 7. Orchestrator routing guard + benchmark (`95757a2c3`, `13c1fc21e`)
**Routing guard:** a `create` from the default front-desk profile must use
`assignee='orchestrator'` (reject-only, no coercion); orchestrator/workers are
exempt. Lifted out of inline `tools/kanban_tools.py` into the opt-in plugin
`plugins/kanban-orchestrator-routing/` as a `pre_tool_call` hook, so
`_handle_create` is now routing-agnostic. **Tradeoff:** opt-in — the deploy
config must enable `kanban-orchestrator-routing` for the invariant to be active
(`KANBAN_ORCHESTRATOR_ROUTING_DISABLE=1` keeps it installed but inert).
**Benchmark:** executable spec for the 3-layer design under
`tests/orchestrator_benchmark/` + `evals/orchestrator_routing/`; GREEN tests
guard current behavior, `xfail(strict)` targets flip once their `kanban_db`
contract functions land.

---

## Front-desk experience

### 8. Replay timestamp prefix (`39a5bf9ff`)
`_format_user_timestamp_prefix` prepends `[YYYY-MM-DD HH:MM TZ]`
(America/Los_Angeles) to replayed **user** messages so the model perceives
send-time and inter-turn gaps. Applied at replay only (not persisted), plain-
string user content only. Home: `gateway/run.py::_build_gateway_agent_history`.

### 11. UX slimming + explicit-skill policy (`0a75a7315`)
Hides internal Kanban plumbing (task ids, worker names, dispatcher/run details,
sub state) from normal Telegram/Weixin replies unless debug detail is requested.
Adds front-desk skill allowlists + tool slimming for messaging surfaces while
preserving broad CLI capability. Explicit/preloaded task skills bypass ambient
`skills.allowed` filters (so lean workers can force-load needed skills), with hard
guardrails via `skills.disabled` / `…forced_denied` (+ platform variants).

### 12. Telegram pre-LLM ack (`0a75a7315`)
Sends an immediate front-desk acknowledgement before the LLM turn so users get
fast feedback on long-running work. See the responsiveness benchmark (#17).

### 17. Responsiveness benchmark + live TTFT (`95772b8f5`)
Deterministic benchmark over emulated user sessions for the front-desk default
profile — drives the real pre-LLM-ack + public-progress policies and scores
time-to-first-feedback. Opt-in live TTFT mode (`run_live.py`) invokes the real
agent and reads ttft/ttfa/ttlt from telemetry. Files: `evals/responsiveness/`,
`tests/responsiveness_benchmark/`, and
`.claude/skills/responsiveness-benchmark/SKILL.md` — the **only** file this fork
tracks under `.claude/` (committed deliberately; relocate or drop if upstreaming).

---

## Context, tools, observability

### 9. AgentFeeds stable manifest (`2ea24d4bb`, `62aa2c45c`)
The AgentFeeds system manifest (stable per-session stream inventory, excludes
volatile freshness/health/content) is appended to `stable_parts` so it lands in
the cache-stable prefix, not the volatile tail. Also fixes a latent `NameError`
(the pre-patch code appended to a non-existent local). Homes: helpers in
`run_agent.py`; wiring in `agent/system_prompt.py::build_system_prompt_parts`
(lazy import, monkeypatch-safe); config init in `agent/agent_init.py`.

### 10. Read-only AgentFeeds toolset (`0a75a7315`)
A narrow `agentfeeds_readonly` toolset (`agentfeeds_read`, `agentfeeds_search`)
over cached AgentFeeds state — no refresh/subscribe/commands/web/file access.
Gives front-desk flows compact AgentFeeds context without the mutating surface.
Key file: `tools/agentfeeds_readonly_tool.py`.

### 13. Segmented telemetry (`0a75a7315`)
`agent/telemetry.py` — segmented dispatch/completion telemetry; surfaces
ttft/ttfa/ttlt used by the responsiveness benchmark's live mode.

### 14. Break-glass (`0a75a7315`)
`hermes_cli/break_glass.py` — operator escape hatch for recovering stuck state.
CLI wiring moved into the `break-glass-cli` plugin via `register_cli_command`, so
`hermes_cli/main.py` is upstream-identical. **Tradeoff:** plugin CLI commands are
opt-in and discovery failures are swallowed — **keep `break-glass-cli` enabled**
(it is, in deploy) so this emergency subcommand can't silently vanish.

### 15. Google Workspace OAuth setup (`0a75a7315`)
Hardening of `skills/productivity/google-workspace/scripts/setup.py` (scope
filtering, JSON auth-url payloads, fresh-auth-url-on-failure).

### 16. Profile-memory dashboard plugin (`ee0334194`)
`plugins/profile-memory/dashboard/` — dashboard plugin for editing profile memory
(plugin_api + bundled UI).

---

## Security & maintenance

### 18. CVE security re-pins (`84ceb225c`) — **re-check every merge**
The `v2026.5.16` merge reverted `pyproject.toml`'s aiohttp/anthropic pins and
dropped the `cryptography` floor. Restored: `aiohttp==3.13.4`
(CVE-2026-34513/34518/34519/34520/34525), `anthropic==0.87.0`
(CVE-2026-34450/34452), `cryptography==46.0.7` (CVE-2026-39892). Re-verify on
every upstream merge (alongside the #4 column check).

### 19. tests/local/ extraction (`894daa376`)
See [Test organization](#test-organization).

---

## Test organization

Local-only test additions live under **`tests/local/`** (mirrors the upstream
layout) so upstream merges stay conflict-free. Upstream test files stay byte-
identical except a small set of irreducible inline edits encoding local behavior
(the full-summary assertion, `request_id` kwargs, env-isolation `delenv` loops,
the two `restart_*` files). This cut the test-file conflict surface from ~1,660
to ~133 lines.

`tests/local/conftest.py` re-exposes the directory-scoped fixtures the extracted
tests rely on (telegram/discord `sys.modules` mocks, `all_assignees_spawnable`,
the run_agent autouse retry-backoff short-circuit). Extracted tests import
upstream helpers from their original modules.

**When adding local tests, put them under `tests/local/<mirror-path>/` rather
than editing upstream test files.**
