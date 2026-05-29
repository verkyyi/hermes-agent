# Local Patches Inventory

What this fork adds on top of upstream Hermes Agent (NousResearch) — one line per
patch, grouped by subsystem. **Bold** marks a flag to re-check on an upstream
merge. Details live in the code and the listed commits, not here.

- **Branches:** `verky/deploy` runs; `main` mirrors `upstream/main` (fast-forward only). Sync by merging `main` *into* `verky/deploy`, never rebase.
- **Baseline:** `upstream/main` @ `2d5dcfabc` (2026-05-27); forked at `v2026.5.16`.
- **Tests:** local-only tests live under `tests/local/` (mirrors upstream) — add new ones there, never edit upstream test files.
- **Merge surface:** `python scripts/merge_surface.py --check N` gates conflict-prone files (`gateway/run.py`, `hermes_cli/kanban_db.py`).

## Kanban delegation & delivery
- **Completion delivery & origin-return** — returns a worker's result to the requesting surface; delivery mode is operator policy (default Telegram→`synthesize`, which re-enters the handoff as a normal origin-session turn); `kanban_decompose` parks the orchestrator's task as the fan-in anchor. Design: `docs/plans/2026-05-28-kanban-wake-origin-session.md`. (`be4900d9d, 0a75a7315, 39a5bf9ff, c653c8881, 417e21530, b60229dec`)
- **Origin-return reliability** — the wake runs outside the conversation lock and as a non-blocking background task; a completing router propagates its subscription to delegated children; create-then-complete is parked as the anchor. (`3849a3663, 12ddd3bd2, e38ecbf47, 33e6526cf`)
- **DB WAL-corruption prevention** — lowers the default `kanban.max_spawn` to 4 (**deploy config overrides to 20**, so the active guards are the other two), enables `PRAGMA checkpoint_fullfsync`, and dedups corrupt-DB backups; fixes the 2026-05-27 checkpoint tear. (`82aa0f767`)
- **Lifecycle recovery + sticky-block** — hardens heartbeat/claim/stale-run/audit; permanent preflight failures park (sticky `blocked`) instead of respawning every tick. (`380eec386, b2301c4d1`)
- **Full handoff summary** — completed-event summary and rendered handoff are no longer truncated. (`39a5bf9ff, 3592c3510`)
- **Notify-sub column guards** — idempotent ALTERs backfill five fork-local columns upstream lacks; **preserve across merges** until PR #21523 lands. (`3592c3510, 7ee088258`)
- **Orchestrator routing guard** — front-desk `create` must target `assignee=orchestrator`; **opt-in plugin — enable `kanban-orchestrator-routing`** or it's off. (`95757a2c3`)
- **Orchestrator hardening benchmark** — executable spec for the 3-layer design (front-desk → orchestrator → workers → fan-in). (`13c1fc21e`)

## Front-desk experience
- **Restart comeback to initiator** — the chat that runs `/restart` always gets the "gateway restarted" reply on comeback; `gateway_restart_notification` now gates only the unsolicited home-channel broadcast (diverges from upstream, which suppressed both). **Touches upstream `tests/gateway/test_restart_notification.py` (one flipped assertion) — re-check on merge.** (`b73853109`)
- **Telegram pre-LLM ack** — immediate acknowledgement before the LLM turn. (`0a75a7315`)
- **Context-aware ack upgrade** — after the deterministic ack sends (<300ms TTFF, unchanged), a detached background task asks the LLM (provider-agnostic auxiliary client, mirroring the main agent's provider/model) for a one-line ack grounded in the user's message and edits the bubble in place (Telegram only — Weixin can't edit). Fully best-effort (any failure leaves the deterministic ack). Config: `responsiveness.llm_ack.{enabled,model,timeout_s}` (`model` empty = use main model); env `HERMES_RESP_LLM_ACK[_MODEL|_TIMEOUT_S]`. E2E: `scripts/e2e/verify_llm_ack_upgrade.py`.
- **Configurable per-outcome reactions** — Telegram lifecycle reactions resolve through a slot map (`progress`/`success`/`failure`/`needs_input`); defaults unchanged from upstream (👀→👍/👎), each overridable via `telegram.reaction_{progress,success,failure,needs_input}` / env `TELEGRAM_REACTION_{PROGRESS,SUCCESS,FAILURE,NEEDS_INPUT}`. An empty value clears instead of sets — deploy uses `reaction_success: ""` so a completed turn clears the 👀 rather than stamp a repetitive 👍. Additive (upstream reaction tests unchanged); local tests in `tests/local/gateway/test_telegram_reactions_config.py`.
- **Needs-input reaction (🤔)** — when the agent pauses on a dangerous-command approval (the turn *blocks* on `/approve`/`/deny`, so the pending state isn't visible at completion), the approval-notify path stamps a mid-turn 🤔 on the triggering message via `TelegramAdapter.on_awaiting_input` (routed through best-effort `GatewayRunner._react_awaiting_input`); the completion hook overwrites it with the final outcome reaction on resolve. Default 🤔, overridable via `reaction_needs_input`. E2E phase D in `scripts/e2e/verify_reactions_and_smart_quote.py`; local tests in `tests/local/gateway/test_awaiting_input_reaction.py`.
- **Context-aware "smart" reply quoting** — new additive `reply_to_mode: smart` suppresses the redundant reply-to quote in a linear 1:1 DM but still quotes the first chunk in groups and on out-of-band DM replies (newer messages arrived mid-turn; tracked via a bounded per-chat last-inbound map). off/first/all unchanged. E2E: `scripts/e2e/verify_reactions_and_smart_quote.py`; local tests in `tests/local/gateway/test_telegram_smart_reply.py`.
- **UX slimming + explicit-skill policy** — hides internal Kanban plumbing from replies; front-desk skill allowlists; preloaded task skills bypass ambient filters (hard guardrails kept). (`0a75a7315`)
- **Responsiveness benchmark + live TTFT** — scores front-desk time-to-first-feedback; ships the only file this fork tracks under `.claude/`. (`95772b8f5`)
- **Replay timestamp prefix** — prepends a wall-clock prefix to replayed user messages (replay-only). (`39a5bf9ff`)

## Context, tools, observability
- **AgentFeeds stable manifest** — manifest in the cache-stable prompt prefix; also fixes a latent NameError. (`2ea24d4bb, 62aa2c45c`)
- **Read-only AgentFeeds toolset** — `agentfeeds_read`/`search` over cached state, no mutation/web/file access. (`0a75a7315`)
- **Segmented agent telemetry** — `agent/telemetry.py`; surfaces ttft/ttfa/ttlt for the benchmark. (`0a75a7315`)
- **HermesBench consolidated benchmark** — one runner (`evals/hermesbench/`) wrapping the fork's four eval harnesses (responsiveness, kanban-scale, orchestrator-routing, origin-return) behind a registry + SQLite trend store (`$HERMES_HOME/hermesbench.db`, rollback-journal not WAL) + a daily summary with deltas vs the prior run. `core` tier is deterministic/daily-safe; `live` tier gated by `HERMES_RUN_LLM_EVALS`. Records git/model/profile fingerprint per run (harness pinning). Dashboard trend view at `/hermesbench` + `GET /api/hermesbench/trend` (additive routes registered before the SPA catch-all; no existing route touched). Category + 3-grading-mode design inspired by ClawBench Core v1. Trend view is a bundled dashboard plugin (`plugins/hermesbench/dashboard/`, tab `/hermesbench` + `/api/plugins/hermesbench/trend`). Daily launchd job `ai.hermes.hermesbench` (host artifact, full-live tier @ 04:00). Run: `python -m evals.hermesbench.run`. Tests: `tests/hermesbench/`. Doc: `evals/hermesbench/README.md`. Backward-compatible one-line tweak to `tests/stress/test_benchmarks.py` (honor `KANBAN_BENCH_OUT`, return results).
- **Break-glass escape hatch** — operator recovery for stuck state; **keep the `break-glass-cli` plugin enabled**. (`0a75a7315`)
- **Google Workspace OAuth hardening** — scope filtering, JSON auth-url payloads, fresh URL on failure. (`0a75a7315`)
- **Profile-memory dashboard plugin** — UI for editing profile memory. (`ee0334194`)
- **Local dashboard-auth provider** — bundled `local` DashboardAuthProvider lets the dashboard bind to a non-loopback host (LAN) with auth but without the Nous Portal `agent:{instance_id}` client_id the `nous` provider needs. Activates only when `dashboard.local_auth.passcode` / `HERMES_DASHBOARD_LOCAL_PASSCODE` is set (fail-closed, like `nous`); sessions are stateless HMAC tokens keyed on the passcode (rotation invalidates them). Adds an **additive** pre-auth `GET/POST /auth/password` form (providers opt in via `password_login=True`) + one middleware allowlist entry + a `render_password_html()`; no existing auth logic modified. Local test: `tests/local/hermes_cli/test_dashboard_auth_local_provider.py`. (`1e547e0ce`)

## Security & maintenance
- **CVE security re-pins** — `aiohttp==3.13.4`, `anthropic==0.87.0`, `cryptography==46.0.7`; **re-check every merge**. (`84ceb225c`)
- **`tests/local/` extraction** — moved local tests out of upstream files to cut merge conflicts. (`894daa376`)
