# Local Patches Inventory

What this fork adds on top of upstream Hermes Agent (NousResearch), grouped by
subsystem. Each entry is one **tag** (Feature / Fix / Hardening / Schema /
Maintenance), one metadata line, and a short description of *what it does and
why* — implementation details live in the code, not here, so this file doesn't
rot on every refactor. **Bold flags** mark anything that must be re-checked on an
upstream merge.

- **Branch model:** `verky/deploy` runs; local `main` mirrors `upstream/main` (fast-forward only, never merge patches into it). Sync by merging `main` *into* `verky/deploy` — never rebase.
- **Baseline:** `upstream/main` @ `2d5dcfabc` (synced 2026-05-27). Forked at `v2026.5.16`.
- **Tests:** local-only tests live under `tests/local/` (mirrors the upstream layout) so merges don't conflict on them — see [Test organization](#test-organization).
- **Merge surface:** `python scripts/merge_surface.py [--check N]` ranks conflict-prone files (CI gate). Hot files `gateway/run.py` and `hermes_cli/kanban_db.py` are fork logic woven into upstream control flow — left in place, resolved at merge, guarded by the Kanban tests.

## At a glance

| Type | Patch | Subsystem |
|------|-------|-----------|
| Feature | Completion delivery & origin-return | Kanban |
| Hardening | Origin-return reliability | Kanban |
| Hardening | DB WAL-corruption prevention | Kanban |
| Hardening | Lifecycle recovery + sticky-block | Kanban |
| Fix | Full handoff summary (no truncation) | Kanban |
| Schema | Notify-sub column guards — **preserve** | Kanban |
| Feature | Orchestrator routing guard (plugin) | Kanban |
| Feature | Orchestrator hardening benchmark | Kanban |
| Feature | Telegram pre-LLM acknowledgement | Front-desk |
| Feature | UX slimming + explicit-skill policy | Front-desk |
| Feature | Responsiveness benchmark + live TTFT | Front-desk |
| Fix | Replay wall-clock timestamp prefix | Front-desk |
| Feature | AgentFeeds stable manifest | Tools/observability |
| Feature | Read-only AgentFeeds toolset | Tools/observability |
| Feature | Segmented agent telemetry | Tools/observability |
| Feature | Break-glass escape hatch (plugin) | Tools/observability |
| Feature | Google Workspace OAuth hardening | Tools/observability |
| Feature | Profile-memory dashboard plugin | Tools/observability |
| Maintenance | CVE security re-pins — **re-check** | Security |
| Maintenance | `tests/local/` extraction | Test org |

---

## Kanban delegation & delivery

### Feature — Completion delivery & origin-return
*Files:* `gateway/kanban_synthesis.py`, `gateway/kanban_notifier.py`, `hermes_cli/kanban_db.py`, `tools/kanban_tools.py` · *Tests:* `tests/local/gateway/test_kanban_notifier.py`, `tests/local/tools/test_kanban_decompose_tool.py` · *Commits:* `be4900d9d, 0a75a7315, 39a5bf9ff, c653c8881, 417e21530, b60229dec`

Returns a background worker's result to the surface that requested it. An
agent-created task records an origin subscription (Telegram/Weixin/CLI); delivery
mode is operator policy (env > `kanban.notify` config > default Telegram→`synthesize`).
In `synthesize` mode a completed task re-enters its handoff into the origin
session as a normal agent turn — no separate gateway-side LLM rewrite.
`kanban_decompose` lets an orchestrator fan out its own task and park it as the
fan-in **anchor** that re-wakes to aggregate. Design:
`docs/plans/2026-05-28-kanban-wake-origin-session.md`.

### Hardening — Origin-return reliability
*Files:* `gateway/kanban_synthesis.py`, `gateway/kanban_notifier.py`, `hermes_cli/kanban_db.py` · *Tests:* `tests/local/gateway/test_kanban_notifier.py`, `tests/local/hermes_cli/test_origin_sub_propagation.py`, `tests/local/hermes_cli/test_self_park_enforce.py` · *Commits:* `3849a3663, 12ddd3bd2, e38ecbf47, 33e6526cf`

Reliability fixes to the path above: the synthesize wake runs outside the
conversation lock (was self-deadlocking) and is dispatched as a background task
so it never blocks the notifier tick; a completing router propagates its
subscription to the children it delegated to; and create-then-complete is
intercepted and parked so the anchor stays the single return point.

### Hardening — DB WAL-corruption prevention
*Files:* `gateway/run.py`, `hermes_cli/kanban_db.py` · *Tests:* `tests/local/hermes_cli/test_kanban_db.py` · *Commits:* `82aa0f767`

Defends `kanban.db` against the 2026-05-27 corruption (a 26-worker burst tore a
WAL checkpoint, then the guard wrote ~2.6 GB of backups). Caps `kanban.max_spawn`
at 4 by default, enables `PRAGMA checkpoint_fullfsync` (macOS flushes through the
drive cache), and dedups corrupt-DB backups so the per-connect retry can't fill
the disk.

### Hardening — Lifecycle recovery + sticky-block
*Files:* `hermes_cli/kanban_db.py` · *Tests:* `tests/hermes_cli/test_kanban_db.py`, `tests/local/hermes_cli/test_kanban_core_functionality.py` · *Commits:* `380eec386, b2301c4d1`

Hardens the run lifecycle (heartbeat/claim/stale-run/audit). A permanent preflight
failure (missing forced skill) emits a sticky `blocked` instead of an
auto-recovered `gave_up`, so it parks for a human rather than respawning every
dispatcher tick — the loop that helped corrupt the DB. Transient failures keep
upstream's auto-recovery (zero upstream test edits).

### Fix — Full handoff summary
*Files:* `hermes_cli/kanban_db.py` · *Tests:* `tests/hermes_cli/test_kanban_core_functionality.py` · *Commits:* `39a5bf9ff, 3592c3510`

The completed-event summary and the rendered handoff are no longer truncated to
the first line / 400 chars — the Kanban DB is the single source of truth.

### Schema — Notify-sub column guards — **preserve across merges**
*Files:* `hermes_cli/kanban_db.py` · *Tests:* `tests/tools/test_kanban_tools.py` · *Commits:* `3592c3510, 7ee088258`

Idempotent `ALTER` guards backfill five fork-local `kanban_notify_subs` columns
that upstream doesn't ship (it has only `notifier_profile`). Without them, a DB
first created on an upstream-schema checkout is missing columns. **Keep across
every upstream merge**; retire only when PR #21523 lands.

### Feature — Orchestrator routing guard (plugin)
*Files:* `plugins/kanban-orchestrator-routing/` · *Tests:* `tests/orchestrator_benchmark/test_frontdesk_routing.py` · *Commits:* `95757a2c3`

A `create` from the default front-desk profile must target `assignee=orchestrator`
(reject-only); orchestrator/workers are exempt. **Opt-in plugin — the deploy
config must enable `kanban-orchestrator-routing` or the invariant is off.**

### Feature — Orchestrator hardening benchmark
*Files:* `tests/orchestrator_benchmark/`, `evals/orchestrator_routing/` · *Commits:* `13c1fc21e`

Executable spec for the 3-layer design (front-desk → orchestrator → workers →
fan-in). GREEN tests guard current behavior; `xfail(strict)` targets track
pending feature work.

---

## Front-desk experience

### Feature — Telegram pre-LLM acknowledgement
*Tests:* `tests/gateway/test_telegram_pre_llm_ack.py` · *Commits:* `0a75a7315`

Sends an immediate acknowledgement before the LLM turn so users get fast feedback
on long-running work.

### Feature — UX slimming + explicit-skill policy
*Tests:* `tests/gateway/test_config.py`, skills tests · *Commits:* `0a75a7315`

Hides internal Kanban plumbing (task ids, workers, run state) from normal
Telegram/Weixin replies unless debug detail is asked for; adds front-desk skill
allowlists. Preloaded task skills bypass ambient filters (hard guardrails kept).

### Feature — Responsiveness benchmark + live TTFT
*Files:* `evals/responsiveness/`, `tests/responsiveness_benchmark/`, `.claude/skills/responsiveness-benchmark/` · *Commits:* `95772b8f5`

Deterministic benchmark scoring front-desk time-to-first-feedback; opt-in live
TTFT mode invokes the real agent. Ships the **only** file this fork tracks under
`.claude/` (relocate or drop if upstreaming).

### Fix — Replay wall-clock timestamp prefix
*Files:* `gateway/run.py` · *Tests:* `tests/gateway/test_user_timestamp_prefix.py` · *Commits:* `39a5bf9ff`

Prepends `[YYYY-MM-DD HH:MM TZ]` to replayed user messages so the model perceives
send-time and inter-turn gaps. Applied at replay only (not persisted).

---

## Context, tools, observability

### Feature — AgentFeeds stable manifest
*Tests:* `tests/local/run_agent/test_agentfeeds.py` · *Commits:* `2ea24d4bb, 62aa2c45c`

Puts the AgentFeeds stream manifest in the cache-stable prompt prefix (not the
volatile tail), improving prompt caching; also fixes a latent `NameError`.

### Feature — Read-only AgentFeeds toolset
*Files:* `tools/agentfeeds_readonly_tool.py` · *Tests:* `tests/tools/test_agentfeeds_readonly_tool.py` · *Commits:* `0a75a7315`

`agentfeeds_read` / `agentfeeds_search` over cached AgentFeeds state — no
mutation, web, or file access. Gives front-desk flows context without the full
mutating surface.

### Feature — Segmented agent telemetry
*Files:* `agent/telemetry.py` · *Tests:* `tests/test_telemetry.py` · *Commits:* `0a75a7315`

Segmented dispatch/completion telemetry; surfaces ttft/ttfa/ttlt for the
responsiveness benchmark's live mode.

### Feature — Break-glass escape hatch (plugin CLI)
*Files:* `hermes_cli/break_glass.py`, `plugins/break-glass-cli/` · *Tests:* `tests/hermes_cli/test_break_glass.py`, `tests/local/hermes_cli/test_break_glass_cli_plugin.py` · *Commits:* `0a75a7315`

Operator escape hatch for recovering stuck state; CLI wired via an opt-in plugin
so `hermes_cli/main.py` stays upstream-identical. **Keep `break-glass-cli`
enabled** so the emergency command can't silently vanish.

### Feature — Google Workspace OAuth hardening
*Tests:* `tests/local/skills/test_google_oauth_setup.py` · *Commits:* `0a75a7315`

Hardens the google-workspace setup script (scope filtering, JSON auth-url
payloads, fresh auth URL on failure).

### Feature — Profile-memory dashboard plugin
*Files:* `plugins/profile-memory/dashboard/` · *Tests:* `tests/plugins/test_profile_memory_dashboard_plugin.py` · *Commits:* `ee0334194`

Dashboard plugin for editing profile memory (plugin_api + bundled UI).

---

## Security & maintenance

### Maintenance — CVE security re-pins — **re-check every merge**
*Files:* `pyproject.toml` · *Commits:* `84ceb225c`

Restored pins an upstream merge had reverted: `aiohttp==3.13.4`,
`anthropic==0.87.0`, `cryptography==46.0.7` (multiple 2026 CVEs). **Re-verify on
every upstream merge**, alongside the notify-sub column-guard check.

### Maintenance — `tests/local/` extraction
*Commits:* `894daa376`

See [Test organization](#test-organization).

---

## Test organization

Local-only tests live under **`tests/local/`** (mirrors the upstream layout) so
merges stay conflict-free. Upstream test files stay byte-identical except a few
irreducible inline edits (full-summary assertion, `request_id` kwargs,
env-isolation `delenv` loops, the `restart_*` files). `tests/local/conftest.py`
re-exposes the directory-scoped fixtures the extracted tests need.

**Add new local tests under `tests/local/<mirror-path>/` — don't edit upstream
test files.**
