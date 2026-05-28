# kanban-orchestrator-routing

Enforces the **front-desk routing invariant** as a `pre_tool_call` hook instead
of an inline edit in `tools/kanban_tools.py`. See `docs/LOCAL_PATCHES.md` #6.

## What it does

A `kanban_create` issued from the **default front-desk profile** must be assigned
to `orchestrator` — the front desk routes everything through the orchestrator and
may not hand work straight to a `worker-*` lane. A non-orchestrator front-desk
assignee is **rejected** (not coerced) so the model re-issues the call.

**Scope.** Fires only when `HERMES_PROFILE` is unset or `"default"` **and** the
session is not dispatcher-spawned (`HERMES_KANBAN_TASK` unset). Exempt: the
orchestrator profile in both modes (dispatcher-spawned and its interactive routing
surface), and every worker — fanning out to worker lanes is their job. Absent /
blank assignee is left alone so the tool's own "assignee is required" error wins.

## Enabling (required)

Standalone plugins are **opt-in**. Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - kanban-orchestrator-routing
```

Until enabled, the invariant is **not enforced** — this is the tradeoff of moving
a guard from always-on core code into a plugin. Set
`KANBAN_ORCHESTRATOR_ROUTING_DISABLE=1` to keep the plugin installed but inert.

## Tests

`tests/orchestrator_benchmark/test_frontdesk_routing.py` — hook scope,
guardrails, the now-routing-agnostic handler, and real plugin-manager wiring +
opt-in gating.
