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
- **DB WAL-corruption prevention** — caps `kanban.max_spawn`=4, enables `PRAGMA checkpoint_fullfsync`, dedups corrupt-DB backups; fixes the 2026-05-27 checkpoint tear. (`82aa0f767`)
- **Lifecycle recovery + sticky-block** — hardens heartbeat/claim/stale-run/audit; permanent preflight failures park (sticky `blocked`) instead of respawning every tick. (`380eec386, b2301c4d1`)
- **Full handoff summary** — completed-event summary and rendered handoff are no longer truncated. (`39a5bf9ff, 3592c3510`)
- **Notify-sub column guards** — idempotent ALTERs backfill five fork-local columns upstream lacks; **preserve across merges** until PR #21523 lands. (`3592c3510, 7ee088258`)
- **Orchestrator routing guard** — front-desk `create` must target `assignee=orchestrator`; **opt-in plugin — enable `kanban-orchestrator-routing`** or it's off. (`95757a2c3`)
- **Orchestrator hardening benchmark** — executable spec for the 3-layer design (front-desk → orchestrator → workers → fan-in). (`13c1fc21e`)

## Front-desk experience
- **Telegram pre-LLM ack** — immediate acknowledgement before the LLM turn. (`0a75a7315`)
- **UX slimming + explicit-skill policy** — hides internal Kanban plumbing from replies; front-desk skill allowlists; preloaded task skills bypass ambient filters (hard guardrails kept). (`0a75a7315`)
- **Responsiveness benchmark + live TTFT** — scores front-desk time-to-first-feedback; ships the only file this fork tracks under `.claude/`. (`95772b8f5`)
- **Replay timestamp prefix** — prepends a wall-clock prefix to replayed user messages (replay-only). (`39a5bf9ff`)

## Context, tools, observability
- **AgentFeeds stable manifest** — manifest in the cache-stable prompt prefix; also fixes a latent NameError. (`2ea24d4bb, 62aa2c45c`)
- **Read-only AgentFeeds toolset** — `agentfeeds_read`/`search` over cached state, no mutation/web/file access. (`0a75a7315`)
- **Segmented agent telemetry** — `agent/telemetry.py`; surfaces ttft/ttfa/ttlt for the benchmark. (`0a75a7315`)
- **Break-glass escape hatch** — operator recovery for stuck state; **keep the `break-glass-cli` plugin enabled**. (`0a75a7315`)
- **Google Workspace OAuth hardening** — scope filtering, JSON auth-url payloads, fresh URL on failure. (`0a75a7315`)
- **Profile-memory dashboard plugin** — UI for editing profile memory. (`ee0334194`)

## Security & maintenance
- **CVE security re-pins** — `aiohttp==3.13.4`, `anthropic==0.87.0`, `cryptography==46.0.7`; **re-check every merge**. (`84ceb225c`)
- **`tests/local/` extraction** — moved local tests out of upstream files to cut merge conflicts. (`894daa376`)
