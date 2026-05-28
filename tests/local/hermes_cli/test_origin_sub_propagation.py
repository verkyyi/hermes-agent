"""Deterministic coverage for origin-subscription propagation on completion.

The reliable half of the origin-return fix (no LLM): when a router/orchestrator
task that carries an origin notify subscription completes while it still has
pending children (it delegated the real work), the subscription moves onto those
children so the user's answer returns from the task that holds it — not from the
router's routing-only summary. Composes with self-park (see
docs/plans/2026-05-28-kanban-wake-origin-session.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _telegram_sub(conn, task_id, chat="6625"):
    kb.add_notify_sub(
        conn, task_id=task_id, platform="telegram", chat_id=chat,
        notification_mode="synthesize",
    )


def test_router_completion_moves_origin_sub_to_pending_children(kanban_home):
    """Orchestrator create+completes: sub moves off the router onto the pending
    child that holds the real work."""
    with kb.connect() as conn:
        t0 = kb.create_task(conn, title="router", assignee="orchestrator")
        _telegram_sub(conn, t0)
        child = kb.create_task(conn, title="real work", assignee="worker", parents=(t0,))

        kb.complete_task(conn, t0, summary="routed to worker (no real answer here)")

        # Router no longer carries the sub; the pending child now does.
        assert kb.list_notify_subs(conn, t0) == []
        child_subs = kb.list_notify_subs(conn, child)
        assert any(
            s["platform"] == "telegram" and s["chat_id"] == "6625" for s in child_subs
        ), f"origin sub did not move to the pending child: {child_subs}"


def test_self_parked_anchor_keeps_sub_on_completion(kanban_home):
    """Self-park/deliverable case: the anchor is a CHILD of the work task (it
    waits for the work), so it parents nobody. When the work finishes and the
    anchor is aggregated + completed, it has no children to move the sub to, so
    the sub stays and the anchor delivers the real answer."""
    with kb.connect() as conn:
        t0 = kb.create_task(conn, title="anchor", assignee="orchestrator")
        _telegram_sub(conn, t0)
        work = kb.create_task(conn, title="real work", assignee="worker")
        # self-park: the anchor t0 is a CHILD of the work task (waits for it)
        kb.link_tasks(conn, parent_id=work, child_id=t0)

        kb.complete_task(conn, work, summary="work done")  # unblocks the anchor
        kb.complete_task(conn, t0, summary="aggregated answer")

        # The anchor parents no one -> sub stays -> it delivers the aggregate.
        assert any(s["platform"] == "telegram" for s in kb.list_notify_subs(conn, t0))


def test_leaf_completion_keeps_its_sub(kanban_home):
    """A leaf task (no children) keeps its subscription and delivers normally."""
    with kb.connect() as conn:
        t0 = kb.create_task(conn, title="leaf", assignee="worker")
        _telegram_sub(conn, t0)
        kb.complete_task(conn, t0, summary="done")
        assert any(s["platform"] == "telegram" for s in kb.list_notify_subs(conn, t0))


def test_no_sub_no_propagation(kanban_home):
    """No origin sub -> nothing to move; completion is unaffected."""
    with kb.connect() as conn:
        t0 = kb.create_task(conn, title="router", assignee="orchestrator")
        child = kb.create_task(conn, title="work", assignee="worker", parents=(t0,))
        assert kb.complete_task(conn, t0, summary="x") is True
        assert kb.list_notify_subs(conn, child) == []
