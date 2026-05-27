# Local Patches Inventory

This fork carries local patches on top of upstream Hermes Agent (NousResearch).
This document inventories them — what each patch is, why it exists, and where its
test coverage lives — so the divergence stays legible across upstream merges.

- **Running branch:** `verky/deploy`
- **Upstream baseline:** `v2026.5.16` (v0.14.0, commit `a91a57fa5`)
- **Upstream mirror:** local `main` tracks `upstream/main`

> **Maintenance workflow.** Keep `main` a pristine upstream mirror — fast-forward
> only (`git fetch upstream && git branch -f main upstream/main`), never merge
> local patches into it. Sync by merging `main` (or an upstream tag) *into*
> `verky/deploy`. Local test additions live under `tests/local/` (mirrors the
> upstream test layout) so upstream merges don't conflict on them — see
> [Test organization](#test-organization). Keep this file current when adding,
> removing, or materially changing a local patch.

## Summary

| # | Area | Commit(s) | Test coverage |
|---|------|-----------|---------------|
| 1 | Kanban origin-return + notification modes (`direct`/`synthesize`/`silent`) | `be4900d9d`, `0a75a7315` | `tests/gateway/test_kanban_notifier.py`, `tests/tools/test_kanban_tools.py` (+ `tests/local/`) |
| 2 | Kanban completion **synthesis**, extracted into `KanbanSynthesisMixin` | `39a5bf9ff` | `tests/gateway/test_kanban_notifier.py` (+ `tests/local/gateway/`) |
| 3 | Full handoff summary in the completed event (no first-line/400-char cap) | `39a5bf9ff`, `3592c3510` | `tests/hermes_cli/test_kanban_core_functionality.py` |
| 4 | Post-v0.14.0 migration-guard cleanup | `3592c3510` | `tests/tools/test_kanban_tools.py` |
| 5 | Kanban lifecycle recovery hardening (heartbeat/claim/stale-run/audit) | `380eec386` | `tests/hermes_cli/test_kanban_db.py` (+ `tests/local/`) |
| 6 | Orchestrator 3-layer routing (front-desk → orchestrator → workers) | `95757a2c3` | `tests/orchestrator_benchmark/test_frontdesk_routing.py` (4) |
| 7 | Orchestrator hardening benchmark (executable TDD spec) | `13c1fc21e` | `tests/orchestrator_benchmark/` + `evals/orchestrator_routing/` (GREEN guards + `xfail(strict)` TDD targets) |
| 8 | Replay wall-clock timestamp prefix on user messages | `39a5bf9ff` | `tests/gateway/test_user_timestamp_prefix.py` (5) |
| 9 | AgentFeeds stable manifest in the cache-stable prompt region | `2ea24d4bb`, `62aa2c45c` | `tests/local/run_agent/test_agentfeeds.py` |
| 10 | Read-only AgentFeeds toolset (`agentfeeds_read`/`agentfeeds_search`) | `0a75a7315` | `tests/tools/test_agentfeeds_readonly_tool.py` |
| 11 | Front-desk UX slimming + explicit-skill policy | `0a75a7315` | `tests/gateway/test_config.py`, skills tests |
| 12 | Telegram pre-LLM acknowledgement (front-desk responsiveness) | `0a75a7315` | `tests/gateway/test_telegram_pre_llm_ack.py` (+ benchmark #16) |
| 13 | Segmented agent telemetry (`agent/telemetry.py`) | `0a75a7315` | `tests/test_telemetry.py` |
| 14 | Break-glass operator escape hatch (`hermes_cli/break_glass.py`) | `0a75a7315` | `tests/hermes_cli/test_break_glass.py` |
| 15 | Google Workspace OAuth setup hardening | `0a75a7315` | `tests/local/skills/test_google_oauth_setup.py` |
| 16 | Profile-memory dashboard plugin | `ee0334194` | `tests/plugins/test_profile_memory_dashboard_plugin.py` |
| 17 | Default-profile responsiveness benchmark + live TTFT | `95772b8f5` | `tests/responsiveness_benchmark/` (28 GREEN + 2 `xfail`) |
| 18 | Re-applied CVE security pins dropped by the v2026.5.16 merge | `84ceb225c` | _none dedicated_ (lockfile/pin change) |
| 19 | `tests/local/` extraction (merge-pain reduction) | `894daa376` | _test-organization meta_ |
| 20 | Notify-subscription upsert (re-subscribe updates mode/origin; no cursor reset) | `c653c8881` | `tests/local/hermes_cli/test_kanban_db.py` |

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
`model_tools.py`, `toolsets.py`.

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

### 4. Migration-guard cleanup (`3592c3510`)
`_migrate_add_optional_columns` drops the ALTER-guard block for
`notification_mode` / `origin_session_id` / `origin_profile` / `origin_context` /
`request_id` — these are in the base `CREATE TABLE` schema as of upstream
v0.14.0. `notifier_profile`'s guard is kept for legacy pre-upstream DBs.

### 5. Lifecycle recovery hardening (`380eec386`)
Hardens kanban run lifecycle: heartbeat extends only the owner's current run,
stale heartbeats don't extend foreign runs, completion-rejection context
identifies stale runs, blocked-task recovery preserves audit history, and
spawn-failure payloads include a log tail.
Key file: `hermes_cli/kanban_db.py`.

### 20. Notify-subscription upsert (`c653c8881`)
`add_notify_sub` uses `INSERT ... ON CONFLICT(task_id,platform,chat_id,thread_id)
DO UPDATE` instead of `INSERT OR IGNORE`: a re-subscribe updates
`notification_mode` and back-fills origin/identity fields (COALESCE-preserving
any the new call omits), never duplicates the row, and never resets the
delivery cursor (`last_event_id`) or `created_at`. Ported from the retired
`agent-driven-kanban-orchestration` branch (task t_cd8321e9); that branch's
other change (per-origin conversation locks) was already in deploy.
Key file: `hermes_cli/kanban_db.py`.

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

---

## Front-desk experience

### 8. Replay timestamp prefix (`39a5bf9ff`)
`gateway/run._format_user_timestamp_prefix` prepends `[YYYY-MM-DD HH:MM TZ]`
(America/Los_Angeles) to replayed **user** messages so the model perceives
send-time and inter-turn gaps. Applied at replay only (not persisted → no
double-prefix); plain-string user content only, multimodal parts untouched.

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
