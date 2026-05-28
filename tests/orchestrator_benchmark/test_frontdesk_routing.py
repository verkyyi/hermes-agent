"""Front-desk routing invariant — default-created tasks must go to orchestrator.

Requirement (Verky, 2026-05-27): a task created from an interactive front-desk
session (no HERMES_KANBAN_TASK in env) must be assigned to `orchestrator`. The
front desk routes *everything* through the orchestrator; it may not hand work
straight to a worker-* lane.

Scope: the guard fires only for the DEFAULT front-desk profile (HERMES_PROFILE
unset/"default", and not dispatcher-spawned). Exempt: the orchestrator profile
in BOTH modes — dispatcher-spawned (HERMES_KANBAN_TASK set) and its interactive
routing surface (HERMES_PROFILE=orchestrator, no task) — plus workers. Fanning
out to worker lanes is their job. (A broader "any session without a task env"
rule was rejected: it wrongly blocked the orchestrator's routing surface.)

Reject-only (not coerce): a non-orchestrator front-desk assignee is blocked so
the model re-issues with assignee='orchestrator'.

ENFORCEMENT LAYER (docs/LOCAL_PATCHES.md #6): the invariant now lives in the
opt-in `kanban-orchestrator-routing` plugin as a `pre_tool_call` hook, NOT as an
inline edit in `tools.kanban_tools._handle_create`. These tests exercise the
plugin hook directly (scope), plus the real plugin-manager wiring + opt-in
gating end-to-end. `_handle_create` itself is now routing-agnostic — proven by
`test_handler_no_longer_guards_routing_inline`.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
import tools.kanban_tools as kt

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_INIT = _REPO_ROOT / "plugins" / "kanban-orchestrator-routing" / "__init__.py"


def _load_plugin():
    """Import the hyphen-named bundled plugin module by file path."""
    spec = importlib.util.spec_from_file_location(
        "kanban_orchestrator_routing_plugin", _PLUGIN_INIT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_plugin = _load_plugin()


# --- hook scope (the invariant) -------------------------------------------


def test_frontdesk_blocks_non_orchestrator_assignee(monkeypatch):
    """Front desk assigning straight to a worker → hook returns a block directive."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)  # interactive front desk
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    out = _plugin._on_pre_tool_call(
        tool_name="kanban_create",
        args={"title": "look up the weather", "assignee": "worker-ops"},
    )
    assert isinstance(out, dict) and out.get("action") == "block", out
    assert "orchestrator" in out["message"].lower()


def test_frontdesk_allows_orchestrator_assignee(monkeypatch):
    """The front desk assigning to orchestrator is always allowed (hook → None)."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    assert (
        _plugin._on_pre_tool_call(
            tool_name="kanban_create",
            args={"title": "look up the weather", "assignee": "orchestrator"},
        )
        is None
    )


def test_dispatcher_spawned_may_assign_to_worker(monkeypatch):
    """Exemption: a dispatcher-spawned agent (HERMES_KANBAN_TASK set) may fan out
    directly to a worker — that's the orchestrator/worker job."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    assert (
        _plugin._on_pre_tool_call(
            tool_name="kanban_create",
            args={"title": "fan out", "assignee": "worker-ops"},
        )
        is None
    )


def test_orchestrator_routing_surface_may_assign_to_worker(monkeypatch):
    """Exemption: the orchestrator's INTERACTIVE routing surface
    (HERMES_PROFILE=orchestrator, no HERMES_KANBAN_TASK) may assign to a worker.

    Guards against regressing the scope back to the over-broad rule.
    """
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    assert (
        _plugin._on_pre_tool_call(
            tool_name="kanban_create",
            args={"title": "route this", "assignee": "worker-research"},
        )
        is None
    )


# --- hook guardrails -------------------------------------------------------


def test_absent_assignee_not_masked(monkeypatch):
    """Absent/blank assignee → hook stays out of it so the tool handler can emit
    its own 'assignee is required' error (clearer than the routing message)."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    for args in ({"title": "x"}, {"title": "x", "assignee": ""}, {"title": "x", "assignee": "   "}):
        assert _plugin._on_pre_tool_call(tool_name="kanban_create", args=args) is None, args


def test_non_create_tool_ignored(monkeypatch):
    """The hook only governs kanban_create; every other tool passes through."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    assert (
        _plugin._on_pre_tool_call(
            tool_name="kanban_update", args={"assignee": "worker-ops"}
        )
        is None
    )


def test_disable_flag_makes_hook_inert(monkeypatch):
    """KANBAN_ORCHESTRATOR_ROUTING_DISABLE=1 keeps the plugin installed but inert."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("KANBAN_ORCHESTRATOR_ROUTING_DISABLE", "1")
    assert (
        _plugin._on_pre_tool_call(
            tool_name="kanban_create",
            args={"title": "x", "assignee": "worker-ops"},
        )
        is None
    )


# --- core handler is now routing-agnostic ---------------------------------


def test_handler_no_longer_guards_routing_inline(kanban_home, monkeypatch):
    """The inline guard was removed from _handle_create — it must NOT reject a
    front-desk worker assignee on its own (the plugin hook owns that now)."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    out = json.loads(kt._handle_create({"title": "look up the weather", "assignee": "worker-ops"}))
    assert out.get("ok"), f"handler must be routing-agnostic now, got {out}"
    conn = kb.connect()
    try:
        assert kb.get_task(conn, out["task_id"]).assignee == "worker-ops"
    finally:
        conn.close()


# --- real plugin-manager wiring + opt-in gating ---------------------------


def _fresh_manager_with_enabled(monkeypatch, tmp_path, enabled):
    """Point HERMES_HOME at a tmp config and force a fresh discovery pass.

    The plugin itself is bundled in <repo>/plugins/, so it is always *discovered*;
    `enabled` controls whether the opt-in allow-list *loads* it.
    """
    import yaml
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": enabled}}), encoding="utf-8"
    )
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    plugins_mod.discover_plugins()
    return plugins_mod


def test_integration_block_when_enabled(monkeypatch, tmp_path):
    """End-to-end: with the plugin enabled, the real pre_tool_call dispatch
    returns the block message for a front-desk worker create."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    plugins_mod = _fresh_manager_with_enabled(
        monkeypatch, tmp_path, ["kanban-orchestrator-routing"]
    )
    msg = plugins_mod.get_pre_tool_call_block_message(
        "kanban_create", {"title": "x", "assignee": "worker-ops"}
    )
    assert msg is not None and "orchestrator" in msg.lower()


def test_integration_no_block_when_not_enabled(monkeypatch, tmp_path):
    """Opt-in gating: with the plugin absent from plugins.enabled, the invariant
    is NOT enforced. This is the safety tradeoff of moving it to a plugin —
    enabling it is the deployment's responsibility."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    plugins_mod = _fresh_manager_with_enabled(monkeypatch, tmp_path, [])
    msg = plugins_mod.get_pre_tool_call_block_message(
        "kanban_create", {"title": "x", "assignee": "worker-ops"}
    )
    assert msg is None
