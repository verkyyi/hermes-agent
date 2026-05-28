# Local Patches Inventory

This fork carries local patches on top of upstream Hermes Agent (NousResearch).
It inventories them **by capability** — grouped under the subsystem they touch
and tagged **Feature** / **Fix** / **Hardening** / **Maintenance** — describing
what each does, why it exists, where its tests live, and any flag that must
survive an upstream merge. Commit hashes are a trailing `commits:` tag for
traceability, not the organizing principle.

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
> in place and resolved at merge time, guarded by the Kanban delivery / lifecycle
> / upsert tests. The reducible part — entirely fork-added methods — has been
> lifted into zero-conflict mixin files (see *Completion delivery & origin-return*).

## At a glance

| Type | Patch | Subsystem |
|------|-------|-----------|
| Feature | Completion delivery & origin-return | Kanban |
| Hardening | Origin-return reliability (deadlock, non-blocking wake, sub-propagation, self-park) | Kanban |
| Fix | Full handoff summary (no truncation) | Kanban |
| Hardening | Lifecycle recovery + permanent-failure sticky-block | Kanban |
| Schema | Notify-sub column guards — **preserve across merges** | Kanban |
| Feature | Orchestrator routing guard (plugin) | Kanban |
| Feature | Orchestrator hardening benchmark | Kanban |
| Feature | Telegram pre-LLM acknowledgement | Front-desk |
| Feature | UX slimming + explicit-skill policy | Front-desk |
| Feature | Responsiveness benchmark + live TTFT | Front-desk |
| Fix | Replay wall-clock timestamp prefix | Front-desk |
| Feature | AgentFeeds stable manifest (+ latent-NameError fix) | Tools/observability |
| Feature | Read-only AgentFeeds toolset | Tools/observability |
| Feature | Segmented agent telemetry | Tools/observability |
| Feature | Break-glass escape hatch (plugin CLI) | Tools/observability |
| Feature | Google Workspace OAuth setup hardening | Tools/observability |
| Feature | Profile-memory dashboard plugin | Tools/observability |
| Maintenance | CVE security re-pins — **re-check every merge** | Security |
| Maintenance | `tests/local/` extraction | Test org |

---

## Kanban delegation & delivery

### Feature — Completion delivery & origin-return
`commits: be4900d9d, 0a75a7315, 39a5bf9ff, c653c8881, 417e21530, 8964a887a, baf695a16, b60229dec`
· `tests: tests/local/gateway/test_kanban_notifier.py, tests/local/hermes_cli/test_kanban_decompose_selfpark_db.py, tests/local/hermes_cli/test_kanban_db.py, tests/local/tools/test_kanban_decompose_tool.py`

The whole design for returning a background worker's result to the surface that
asked for it. See `docs/plans/2026-05-28-kanban-wake-origin-session.md` (the
response to upstream PR #21523 being closed).

- **Subscription & routing.** An agent-created task records an origin
  notification subscription (Telegram chat/thread, Weixin, CLI). One tracking
  task = one subscription = one return. `add_notify_sub` upserts on
  `(task,platform,chat,thread)` — a re-subscribe updates the mode and COALESCE-
  backfills identity fields, never duplicating the row or resetting the delivery
  cursor.
- **Delivery mode is operator policy, not a per-task column.**
  `_resolve_kanban_notify_mode` reads env `HERMES_KANBAN_NOTIFY_MODE` >
  `kanban.notify.<platform>.mode` > `kanban.notify.mode` > built-in default
  (Telegram → `synthesize`, else `direct`). Modes: `direct` (raw handoff),
  `synthesize` (wake the origin session), `silent` (internal fan-out children).
  The five fork-local origin/mode columns are now **vestigial for delivery** —
  kept in-schema for back-compat but no longer read to decide delivery, removing
  the footgun of a model setting `silent` on a user-visible task.
- **Wake-origin-session (`synthesize`).** Rather than a gateway-side LLM rewrite
  (the old ~310-line apparatus, deleted), a `completed` event re-enters the
  worker handoff into the origin gateway session as a synthetic `internal=True`
  turn via `_handle_message` (the proven `_process_handoff` pattern) — the origin
  profile composes and delivers the reply through the normal agent loop, with
  full tool access; no second rendering engine. `_wake_with_fallback` bounds the
  wake by `kanban.notify.wake_timeout_seconds` and falls back to the direct
  status line. Artifact upload is skipped in `synthesize` mode (the woken agent
  surfaces artifacts itself — no double-upload).
- **Decompose-anchor self-park.** `kanban_decompose` generalizes upstream
  `decompose_triage_task` with an `allow_running` flag so an orchestrator can fan
  out *its own* in-flight task and park it as the fan-in **anchor**
  (`running → todo`, run ended via `_end_run(outcome="decomposed")`, task-level
  claim cleared so the clean exit isn't flagged as a crash). The anchor
  re-promotes and re-dispatches the orchestrator once all children finish, to
  judge/aggregate. The kanban-orchestrator skill + `KANBAN_GUIDANCE` prefer it.

**Merge-surface homes.** The notifier (`_kanban_notifier_watcher` et al.) lives
in `gateway/kanban_notifier.py::KanbanNotifierMixin`; the delivery/wake half in
`gateway/kanban_synthesis.py::KanbanSynthesisMixin`; fork-added `GatewayRunner`
methods (conversation locks, metrics) in
`gateway/gateway_forklocal.py::ForkLocalGatewayMixin` —
`class GatewayRunner(KanbanSynthesisMixin, KanbanNotifierMixin, ForkLocalGatewayMixin)`.
Byte-movable extractions that keep the hot `gateway/run.py` smaller. Other key
files: `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, `toolsets.py`,
`agent/prompt_builder.py`, `skills/devops/kanban-orchestrator/SKILL.md`.

> A plugin/hook rewrite was rejected: this is a cross-process background reactor
> needing gateway internals (background-task registration, adapter/mirror access,
> a `kanban_event` hook) the plugin contract doesn't expose. **Deferred for an
> upstream PR:** dropping the vestigial columns (schema collapse to
> upstream-identical) and Path-B (front-desk aggregation).

### Hardening — Origin-return reliability
`commits: 3849a3663, 12ddd3bd2, e38ecbf47, 33e6526cf`
· `tests: tests/local/gateway/test_kanban_notifier.py, tests/local/hermes_cli/test_origin_sub_propagation.py, tests/local/hermes_cli/test_self_park_enforce.py` (+ gated real-LLM e2e `evals/origin_return/run.py`)

Reliability fixes to the delivery path above, mostly surfaced by the real-LLM
e2e (run *outside* pytest — `hermes_cli/auth.py` blocks real creds under
`PYTEST_CURRENT_TEST`):

- **Wake self-deadlock.** `_send_kanban_notification` wrapped the wake in the
  per-session conversation lock that the wake's own `_handle_message`
  re-acquires → 180s timeout → silent degrade to a direct send. Fixed by running
  the wake outside that lock (it self-serializes).
- **Non-blocking wake dispatch.** The wake runs a full front-desk agent turn, so
  awaiting it inline blocked the ~5s notifier tick — every other
  board/subscription/heartbeat delivery stalled behind one chat's turn. The wake
  is now dispatched as a tracked background task (`_background_tasks`) and the
  notifier returns immediately; `_run_kanban_wake_delivery` owns the outcome the
  watcher can't observe synchronously (on success: reset failure counter,
  unsubscribe a terminal task; on total failure — wake *and* direct fallback both
  fail: rewind the claim cursor for retry, or drop a dead sub after
  `MAX_SEND_FAILURES`). Dedup is safe because the cursor advances at claim time.
- **Sub-propagation onto children.** When a task carrying a sub completes with
  pending children it delegated to, the sub moves onto those children so the
  answer is never stranded on a router.
- **Enforced self-park.** Guidance alone won't stop a create-then-complete, so
  `kanban_complete` intercepts a return-anchor with pending delegated children
  and parks it as a fan-in anchor — the anchor stays the single return point
  whether the orchestrator used `kanban_decompose` or create+complete.

> Worth a one-task Telegram smoke-test after deploy for the full live
> `park → re-dispatch → aggregate → wake → deliver` loop.

### Fix — Full handoff summary
`commits: 39a5bf9ff, 3592c3510` · `tests: tests/hermes_cli/test_kanban_core_functionality.py`

`complete_task` no longer slices the completed-event summary to the first line /
400 chars, and the notifier no longer truncates the rendered handoff. The Kanban
DB is the single source of truth; downstream readers (Telegram, wake, dashboard
WS) must see the complete handoff. (A deletion in upstream code — nothing to
extract; resolve at merge.)

### Schema — Notify-sub column guards — **preserve across merges**
`commits: 3592c3510, corrected in 7ee088258` · `tests: tests/tools/test_kanban_tools.py`

`_migrate_add_optional_columns` keeps idempotent `ALTER TABLE … ADD COLUMN`
guards for the five fork-local columns (`notification_mode`, `origin_session_id`,
`origin_profile`, `origin_context`, `request_id`) plus `notifier_profile`.

> These columns are **fork-local** — upstream `main` (`2d5dcfabc`) ships
> `kanban_notify_subs` with only `notifier_profile` (they exist upstream only on
> the unmerged PR branch `pr-21523`). `CREATE TABLE IF NOT EXISTS` is a no-op on a
> DB first created by an upstream/older-fork checkout, so the ALTER guards are the
> **only** mechanism that backfills them. **Preserve across every upstream
> merge** (upstream won't supply them); retire only once PR #21523 lands — keep on
> the *CVE re-pins* per-merge re-verification list.

### Hardening — Lifecycle recovery + permanent-failure sticky-block
`commits: 380eec386, b2301c4d1` · `tests: tests/hermes_cli/test_kanban_db.py, tests/local/hermes_cli/test_kanban_core_functionality.py`

Hardens kanban run lifecycle: heartbeats extend only the owner's current run,
stale heartbeats don't extend foreign runs, completion-rejection context
identifies stale runs, blocked-task recovery preserves audit history, and
spawn-failure payloads include a log tail. Key file: `hermes_cli/kanban_db.py`.

> **Sticky-block.** A preflight skill failure (missing/disabled *forced* skill)
> can never succeed on retry, so the circuit breaker emits a sticky `blocked` (via
> `permanent=True` threaded through `_record_spawn_failure` →
> `_record_task_failure`) instead of auto-recoverable `gave_up` —
> `recompute_ready` / `_has_sticky_block` park it for a human instead of
> respawning every dispatcher tick (the loop that helped corrupt `kanban.db` on
> 2026-05-27 at ~2.4 spawn/s). Scoped to the preflight call site only: transient
> crash/timeout still emit `gave_up` and keep upstream's auto-recovery — **zero
> upstream test edits.** Recovery: fix the skill/profile + `hermes kanban
> unblock`, or `archive`.

### Feature — Orchestrator routing guard (plugin)
`commits: 95757a2c3` · `tests: tests/orchestrator_benchmark/test_frontdesk_routing.py`

A `create` from the default front-desk profile must use `assignee='orchestrator'`
(reject-only, no coercion); orchestrator/workers are exempt. Lifted out of inline
`tools/kanban_tools.py` into the opt-in plugin
`plugins/kanban-orchestrator-routing/` as a `pre_tool_call` hook, so
`_handle_create` is now routing-agnostic.

> **Tradeoff — opt-in:** the deploy config must enable
> `kanban-orchestrator-routing` for the invariant to be active
> (`KANBAN_ORCHESTRATOR_ROUTING_DISABLE=1` keeps it installed but inert).

### Feature — Orchestrator hardening benchmark
`commits: 13c1fc21e` · `tests: tests/orchestrator_benchmark/, evals/orchestrator_routing/`

Executable spec for the 3-layer design (default → orchestrator → workers →
fan-in): linking/ownership, block grouped-notify, transient auto-recovery,
garbage auto-archive, e2e lifecycle, LLM routing eval. GREEN tests guard current
behavior; `xfail(strict)` targets flip once their `kanban_db` contract functions
land.

---

## Front-desk experience

### Feature — Telegram pre-LLM acknowledgement
`commits: 0a75a7315` · `tests: tests/gateway/test_telegram_pre_llm_ack.py`

Sends an immediate front-desk acknowledgement before the LLM turn so users get
fast feedback on long-running work. See the responsiveness benchmark.

### Feature — UX slimming + explicit-skill policy
`commits: 0a75a7315` · `tests: tests/gateway/test_config.py, skills tests`

Hides internal Kanban plumbing (task ids, worker names, dispatcher/run details,
sub state) from normal Telegram/Weixin replies unless debug detail is requested.
Adds front-desk skill allowlists + tool slimming for messaging surfaces while
preserving broad CLI capability. Explicit/preloaded task skills bypass ambient
`skills.allowed` filters (so lean workers can force-load needed skills), with hard
guardrails via `skills.disabled` / `…forced_denied` (+ platform variants).

### Feature — Responsiveness benchmark + live TTFT
`commits: 95772b8f5` · `tests: tests/responsiveness_benchmark/`

Deterministic benchmark over emulated user sessions for the front-desk default
profile — drives the real pre-LLM-ack + public-progress policies and scores
time-to-first-feedback. Opt-in live TTFT mode (`run_live.py`) invokes the real
agent and reads ttft/ttfa/ttlt from telemetry. Files: `evals/responsiveness/`,
`tests/responsiveness_benchmark/`, and
`.claude/skills/responsiveness-benchmark/SKILL.md` — the **only** file this fork
tracks under `.claude/` (committed deliberately; relocate or drop if upstreaming).

### Fix — Replay wall-clock timestamp prefix
`commits: 39a5bf9ff` · `tests: tests/gateway/test_user_timestamp_prefix.py`

`_format_user_timestamp_prefix` prepends `[YYYY-MM-DD HH:MM TZ]`
(America/Los_Angeles) to replayed **user** messages so the model perceives
send-time and inter-turn gaps. Applied at replay only (not persisted), plain-
string user content only. Home: `gateway/run.py::_build_gateway_agent_history`.

---

## Context, tools, observability

### Feature — AgentFeeds stable manifest (+ latent-NameError fix)
`commits: 2ea24d4bb, 62aa2c45c` · `tests: tests/local/run_agent/test_agentfeeds.py`

The AgentFeeds system manifest (stable per-session stream inventory, excludes
volatile freshness/health/content) is appended to `stable_parts` so it lands in
the cache-stable prefix, not the volatile tail. Also fixes a latent `NameError`
(the pre-patch code appended to a non-existent local). Homes: helpers in
`run_agent.py`; wiring in `agent/system_prompt.py::build_system_prompt_parts`
(lazy import, monkeypatch-safe); config init in `agent/agent_init.py`.

### Feature — Read-only AgentFeeds toolset
`commits: 0a75a7315` · `tests: tests/tools/test_agentfeeds_readonly_tool.py`

A narrow `agentfeeds_readonly` toolset (`agentfeeds_read`, `agentfeeds_search`)
over cached AgentFeeds state — no refresh/subscribe/commands/web/file access.
Gives front-desk flows compact AgentFeeds context without the mutating surface.
Key file: `tools/agentfeeds_readonly_tool.py`.

### Feature — Segmented agent telemetry
`commits: 0a75a7315` · `tests: tests/test_telemetry.py`

`agent/telemetry.py` — segmented dispatch/completion telemetry; surfaces
ttft/ttfa/ttlt used by the responsiveness benchmark's live mode.

### Feature — Break-glass escape hatch (plugin CLI)
`commits: 0a75a7315` · `tests: tests/hermes_cli/test_break_glass.py, tests/local/hermes_cli/test_break_glass_cli_plugin.py`

`hermes_cli/break_glass.py` — operator escape hatch for recovering stuck state.
CLI wiring moved into the `break-glass-cli` plugin via `register_cli_command`, so
`hermes_cli/main.py` is upstream-identical.

> **Tradeoff:** plugin CLI commands are opt-in and discovery failures are
> swallowed — **keep `break-glass-cli` enabled** (it is, in deploy) so this
> emergency subcommand can't silently vanish.

### Feature — Google Workspace OAuth setup hardening
`commits: 0a75a7315` · `tests: tests/local/skills/test_google_oauth_setup.py`

Hardening of `skills/productivity/google-workspace/scripts/setup.py` (scope
filtering, JSON auth-url payloads, fresh-auth-url-on-failure).

### Feature — Profile-memory dashboard plugin
`commits: ee0334194` · `tests: tests/plugins/test_profile_memory_dashboard_plugin.py`

`plugins/profile-memory/dashboard/` — dashboard plugin for editing profile memory
(plugin_api + bundled UI).

---

## Security & maintenance

### Maintenance — CVE security re-pins — **re-check every merge**
`commits: 84ceb225c` · `tests: none dedicated (lockfile/pin)`

The `v2026.5.16` merge reverted `pyproject.toml`'s aiohttp/anthropic pins and
dropped the `cryptography` floor. Restored: `aiohttp==3.13.4`
(CVE-2026-34513/34518/34519/34520/34525), `anthropic==0.87.0`
(CVE-2026-34450/34452), `cryptography==46.0.7` (CVE-2026-39892). Re-verify on
every upstream merge (alongside the notify-sub column-guard check).

### Maintenance — `tests/local/` extraction
`commits: 894daa376`

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
