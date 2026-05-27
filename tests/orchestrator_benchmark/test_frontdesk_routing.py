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
rule was rejected: it wrongly blocked the orchestrator's routing surface, which
the existing tests/tools/test_kanban_tools.py suite caught.)

Reject-only (not coerce): a non-orchestrator front-desk assignee returns a
tool_error so the model re-issues with assignee='orchestrator'.

These exercise the tool handler `tools.kanban_tools._handle_create` directly,
matching the convention in tests/tools/test_kanban_tools.py.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
import tools.kanban_tools as kt


def test_frontdesk_rejects_non_orchestrator_assignee(kanban_home, monkeypatch):
    """[TDD] Front desk assigning straight to a worker → rejected with a tool_error."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)  # interactive front desk
    out = json.loads(kt._handle_create({"title": "look up the weather", "assignee": "worker-ops"}))
    assert out.get("error"), f"expected rejection, got {out}"
    assert "orchestrator" in out["error"].lower()


def test_frontdesk_allows_orchestrator_assignee(kanban_home, monkeypatch):
    """[GREEN] The front desk assigning to orchestrator is always allowed."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kt._handle_create({"title": "look up the weather", "assignee": "orchestrator"}))
    assert out.get("ok"), f"orchestrator assignee must be allowed, got {out}"
    conn = kb.connect()
    try:
        assert kb.get_task(conn, out["task_id"]).assignee == "orchestrator"
    finally:
        conn.close()


def test_dispatcher_spawned_may_assign_to_worker(kanban_home, monkeypatch):
    """[GREEN] Exemption: a dispatcher-spawned agent (HERMES_KANBAN_TASK set) may
    fan out directly to a worker — that's the orchestrator/worker job."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    out = json.loads(kt._handle_create({"title": "fan out", "assignee": "worker-ops"}))
    assert out.get("ok"), f"dispatcher-spawned create must be exempt, got {out}"
    conn = kb.connect()
    try:
        assert kb.get_task(conn, out["task_id"]).assignee == "worker-ops"
    finally:
        conn.close()


def test_orchestrator_routing_surface_may_assign_to_worker(kanban_home, monkeypatch):
    """[GREEN] Exemption: the orchestrator's INTERACTIVE routing surface
    (HERMES_PROFILE=orchestrator, no HERMES_KANBAN_TASK) may assign to a worker.

    This is the case the over-broad "any session without a task env" rule wrongly
    blocked. Guards against regressing the scope back to A.
    """
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    out = json.loads(kt._handle_create({"title": "route this", "assignee": "worker-research"}))
    assert out.get("ok"), f"orchestrator routing surface must be exempt, got {out}"
    conn = kb.connect()
    try:
        assert kb.get_task(conn, out["task_id"]).assignee == "worker-research"
    finally:
        conn.close()
