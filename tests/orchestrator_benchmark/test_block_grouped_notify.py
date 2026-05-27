"""Orchestrator benchmark — Suite C: block + group-by-ownership notification.

The requirement (Verky, 2026-05-27): when sub-tasks block, notify the human
who initiated the request, GROUPED BY THE OWNERSHIP TREE — one message per
root task listing that root's blocked descendants. NOT one combined blob of
every blocked task, and NOT per-task spam. Separate roots -> separate messages.

Tagging:
    [GREEN]  regression — behavior that exists today.
    [TDD]    target — xfail(strict=True); API not built yet.

Deterministic, kb-level. The grouping/cadence primitives live in kanban_db so
the gateway notifier (async, fake-adapter) only has to call them; the async
delivery wiring is exercised in the Suite F integration test.
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb

# kanban_home fixture is provided by conftest.py in this directory.


def _block(conn, tid, reason):
    # block_task accepts ready|running; a parentless task is created ready.
    assert kb.block_task(conn, tid, reason=reason), f"could not block {tid}"


# ---------------------------------------------------------------------------
# C1 / C2 — existing notification rail (regression guards)
# ---------------------------------------------------------------------------

def test_C1_block_emits_single_blocked_event(kanban_home):
    """[GREEN] Blocking a task emits exactly one 'blocked' event carrying the reason."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", body="x" * 600, assignee="worker-ops")
        _block(conn, tid, "needs a Google Maps API key")
        blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        assert len(blocked) == 1
        assert "Maps API key" in (blocked[0].payload or {}).get("reason", "")
    finally:
        conn.close()


def test_C2_subscription_survives_block(kanban_home):
    """[GREEN] A block does NOT drop the notify subscription.

    The delivery channel must stay alive so the human can be re-nudged while
    the task remains blocked (gateway only unsubscribes on done/archived).
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", body="x" * 600, assignee="worker-ops")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="6625666157")
        _block(conn, tid, "needs OAuth")
        assert len(kb.list_notify_subs(conn, task_id=tid)) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C3 / C4 — group by ownership tree
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="TDD: kb.blocked_grouped_by_root() not yet implemented")
def test_C3_two_blocks_same_root_group_together(kanban_home):
    """[TDD] Two blocked sub-tasks under one root -> a single group keyed by that root."""
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="request", assignee="orchestrator")
        a = kb.create_task(conn, title="sub a", body="x" * 600, assignee="worker-research", parents=[root])
        b = kb.create_task(conn, title="sub b", body="x" * 600, assignee="worker-ops", parents=[root])
        # promote children to ready so they can be claimed/blocked
        kb.complete_task(conn, root, result="routed")  # root done -> children ready
        kb.recompute_ready(conn)
        _block(conn, a, "needs API key")
        _block(conn, b, "needs OAuth")

        groups = kb.blocked_grouped_by_root(conn)
        assert root in groups
        ids = {t.id for t in groups[root]}
        assert ids == {a, b}
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.blocked_grouped_by_root() not yet implemented")
def test_C4_different_roots_stay_separate(kanban_home):
    """[TDD] Blocks under different roots produce SEPARATE groups (the 'not combined' rule)."""
    conn = kb.connect()
    try:
        r1 = kb.create_task(conn, title="request 1", assignee="orchestrator")
        r2 = kb.create_task(conn, title="request 2", assignee="orchestrator")
        a = kb.create_task(conn, title="a", body="x" * 600, assignee="worker", parents=[r1])
        b = kb.create_task(conn, title="b", body="x" * 600, assignee="worker", parents=[r2])
        kb.complete_task(conn, r1, result="routed")
        kb.complete_task(conn, r2, result="routed")
        kb.recompute_ready(conn)
        _block(conn, a, "blocked a")
        _block(conn, b, "blocked b")

        groups = kb.blocked_grouped_by_root(conn)
        assert set(groups.keys()) == {r1, r2}
        assert {t.id for t in groups[r1]} == {a}
        assert {t.id for t in groups[r2]} == {b}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C5 / C6 — re-nudge cadence + dedup
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="TDD: kb.due_for_block_reminder()/record_block_reminder() not yet implemented")
def test_C5_renudge_cadence(kanban_home):
    """[TDD] Re-nudge is due initially, suppressed right after sending, due again after cadence."""
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="request", assignee="orchestrator")
        now = int(time.time())
        # never nudged -> due
        assert kb.due_for_block_reminder(conn, root, cadence_days=(1, 2, 4, 7), now=now) is True
        kb.record_block_reminder(conn, root, now=now)
        # just nudged -> not due an hour later
        assert kb.due_for_block_reminder(conn, root, cadence_days=(1, 2, 4, 7), now=now + 3600) is False
        # past the first cadence step (1d) -> due again
        assert kb.due_for_block_reminder(conn, root, cadence_days=(1, 2, 4, 7), now=now + 2 * 86400) is True
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.record_block_reminder() not yet implemented")
def test_C6_reminder_recorded_as_event(kanban_home):
    """[TDD] record_block_reminder leaves a 'block_reminder' event (the dedup state)."""
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="request", assignee="orchestrator")
        kb.record_block_reminder(conn, root, now=int(time.time()))
        kinds = [e.kind for e in kb.list_events(conn, root)]
        assert "block_reminder" in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C7 — stranded tasks (no live subscription)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="TDD: kb.blocked_roots_without_sub() not yet implemented")
def test_C7_stranded_roots_detected(kanban_home):
    """[TDD] A blocked root with no notify sub is surfaced for re-subscription to default origin."""
    conn = kb.connect()
    try:
        with_sub = kb.create_task(conn, title="has sub", body="x" * 600, assignee="worker-ops")
        kb.add_notify_sub(conn, task_id=with_sub, platform="telegram", chat_id="6625666157")
        _block(conn, with_sub, "needs key")

        stranded = kb.create_task(conn, title="no sub", body="x" * 600, assignee="worker-ops")
        _block(conn, stranded, "needs key")

        roots = kb.blocked_roots_without_sub(conn)
        assert stranded in roots
        assert with_sub not in roots
    finally:
        conn.close()
