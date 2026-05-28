"""Deterministic coverage for reliable self-park enforcement.

``park_as_fanin_anchor`` (and the ``kanban_complete`` interception that calls it)
turns a router task that tries to complete-while-delegating into a fan-in
anchor: the gated children are reversed (they run now, the anchor waits on
them), the anchor is parked at ``todo`` (NOT done) keeping its origin
subscription, and it re-wakes to aggregate + complete once the children finish.
See docs/plans/2026-05-28-kanban-wake-origin-session.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
import tools.kanban_tools as kt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_anchor_with_gated_child_and_sub(conn):
    """Anchor t0 (running, origin sub) + a child gated on it (the create+complete
    shape: ``kanban_create(parents=[t0])``)."""
    t0 = kb.create_task(conn, title="anchor", assignee="orchestrator")
    kb.recompute_ready(conn)
    kb.claim_task(conn, t0)  # -> running
    kb.add_notify_sub(conn, task_id=t0, platform="telegram", chat_id="6625",
                      notification_mode="synthesize")
    child = kb.create_task(conn, title="real work", assignee="worker", parents=(t0,))
    return t0, child


def test_park_reverses_links_and_keeps_anchor_alive(kanban_home):
    with kb.connect() as conn:
        t0, child = _running_anchor_with_gated_child_and_sub(conn)
        n = kb.park_as_fanin_anchor(conn, t0)

        assert n == 1
        anchor = kb.get_task(conn, t0)
        assert anchor.status == "todo"          # parked, NOT done
        assert anchor.current_run_id is None    # active run ended
        assert anchor.worker_pid is None
        # Link reversed: anchor now waits for the child; child no longer gated by anchor.
        assert child in kb.parent_ids(conn, t0)
        assert t0 not in kb.parent_ids(conn, child)
        # Child ungated -> ready; origin sub retained ON THE ANCHOR.
        assert kb.get_task(conn, child).status == "ready"
        assert any(s["platform"] == "telegram" for s in kb.list_notify_subs(conn, t0))


def test_park_is_noop_without_pending_children(kanban_home):
    with kb.connect() as conn:
        t0 = kb.create_task(conn, title="leaf", assignee="orchestrator")
        kb.add_notify_sub(conn, task_id=t0, platform="telegram", chat_id="6625")
        assert kb.park_as_fanin_anchor(conn, t0) == 0
        # untouched: still has its sub, not parked into a waiting state
        assert any(s["platform"] == "telegram" for s in kb.list_notify_subs(conn, t0))


def test_parked_anchor_repromotes_and_delivers_on_completion(kanban_home):
    """Full lifecycle: park -> child finishes -> anchor re-promotes -> aggregate
    + complete -> anchor done WITH the sub still on it (it delivers the answer)."""
    with kb.connect() as conn:
        t0, child = _running_anchor_with_gated_child_and_sub(conn)
        kb.park_as_fanin_anchor(conn, t0)

        kb.complete_task(conn, child, summary="found 3 events")
        # Anchor's only parent (the child) is now done -> anchor re-promotes.
        assert kb.get_task(conn, t0).status == "ready"

        # Orchestrator re-dispatched: aggregates + completes. Children are done
        # (no pending), so the sub stays on the anchor and it delivers.
        assert kb.complete_task(conn, t0, summary="here are the events") is True
        assert kb.get_task(conn, t0).status == "done"
        assert any(s["platform"] == "telegram" for s in kb.list_notify_subs(conn, t0))


def test_kanban_complete_tool_parks_router_instead_of_completing(kanban_home, monkeypatch):
    """The kanban_complete handler intercepts create+complete: a return-anchor
    with a pending delegated child is parked, not completed."""
    with kb.connect() as conn:
        t0, child = _running_anchor_with_gated_child_and_sub(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", t0)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")

    out = kt._handle_complete({"summary": "routed to worker-research"})
    payload = json.loads(out)
    assert payload.get("parked") is True
    assert payload.get("fanin_children") == 1

    with kb.connect() as conn:
        assert kb.get_task(conn, t0).status == "todo"        # parked, not done
        assert kb.get_task(conn, child).status == "ready"    # child ungated
        assert any(s["platform"] == "telegram" for s in kb.list_notify_subs(conn, t0))


def test_kanban_complete_tool_completes_normally_when_no_sub(kanban_home, monkeypatch):
    """A normal worker task (no origin sub) completes as usual — park doesn't fire."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="worker task", assignee="worker")
        kb.recompute_ready(conn)
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    out = kt._handle_complete({"summary": "did the work"})
    payload = json.loads(out)
    assert payload.get("ok") is True and not payload.get("parked")
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
