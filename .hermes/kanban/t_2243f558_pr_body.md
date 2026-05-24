# Agent-driven Kanban orchestration: return task completions to the origin

## Summary

This PR wires model/tool-created Kanban tasks into the same origin-notification loop that slash/CLI-created tasks already use, so agent-driven orchestration can close the loop back to the originating user surface.

It supports the workflow discussed in Discord:

> Natural language request in Telegram → orchestrator creates child tasks → workers execute → results return somewhere useful for the orchestrator/user → orchestrator continues or summarizes.

## What changed

- Adds notification provenance to `kanban_notify_subs`:
  - `notification_mode`: `direct`, `synthesize`, or `silent`
  - `origin_session_id`
  - `origin_profile`
  - `origin_context`
- Auto-subscribes model/tool-created root tasks to the originating routable surface when origin metadata is available.
  - CLI-origin tasks use the session id as the return target.
  - Telegram-origin tasks require the actual chat id; a session id alone is treated as provenance, not a delivery address.
- Lets parentless worker-created recovery/root follow-up tasks inherit the current task’s origin subscription.
  - Normal worker fan-out children linked via `parents` remain silent.
- Adds `notification_mode` to `kanban_create`:
  - `direct`: send the worker handoff directly.
  - `synthesize`: run a no-tools, one-turn origin-profile synthesis over the durable handoff and origin context.
  - `silent`: advance/unsubscribe without sending a terminal notification.
- Treats adapter `SendResult(success=False)` as a notifier delivery failure, so the notifier retries instead of silently advancing the cursor and dropping the user-facing completion.
- Mirrors successful Kanban notifications back into gateway session history for continuity.
- Makes the `kanban` toolset selectable in `hermes tools`, matching the existing explicit-toolset gating in `tools/kanban_tools.py`.
- Adds targeted coverage for notification subscription inheritance, auto-subscribe behavior, `SendResult` failure handling, synthesis fallback, silent mode, and notifier ownership gating.

## Design notes

This deliberately keeps worker completion as durable board state first. Synthesis is a notification mode layered on top of the completed run/event payload and stored origin context; the worker does not directly mutate the origin conversation.

Completion absorption into an active orchestrator session is not included here. That seems design-sensitive and may deserve a follow-up issue/discussion around whether the orchestrator should continue automatically, whether completions should be injected as session messages, and how to avoid recursive task creation.

## Excluded intentionally

This PR excludes local deployment/personal patches, profile/tool allowlists, AgentFeeds/front-desk tools, local config, cron/reporting changes, dashboard/deployment notes, and README fork-local documentation.

## Tests

- `env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_RUN_ID -u HERMES_KANBAN_CLAIM_LOCK -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_HOME -u HERMES_KANBAN_WORKSPACES_ROOT python -m pytest tests/tools/test_kanban_tools.py tests/gateway/test_kanban_notifier.py -q`
- `python -m py_compile gateway/run.py hermes_cli/kanban_db.py hermes_cli/tools_config.py model_tools.py run_agent.py tools/kanban_tools.py toolsets.py tests/tools/test_kanban_tools.py tests/gateway/test_kanban_notifier.py`
- `git diff --check`
- staged diff secret/personal-term scan: no findings
