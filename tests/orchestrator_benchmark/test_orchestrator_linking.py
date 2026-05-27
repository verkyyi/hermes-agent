"""Orchestrator benchmark — Suites A (linking/tree), D (auto-recovery), E (auto-archive).

This is the executable spec for the Hermes Kanban orchestrator hardening
(see design discussion 2026-05-27). Tests are tagged in their docstrings:

    [GREEN]  regression — asserts behavior that exists today.
    [TDD]    target — marked xfail(strict=True). The referenced API does not
             exist yet; the test documents the contract. When implemented and
             passing, strict-xfail flips XPASS -> failure, forcing removal of
             the marker. That is the "this feature is done" signal.

Deterministic only: no LLM, no gateway. Orchestrator *routing judgment*
(Suite B) is a separate LLM eval and lives under evals/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# kanban_home fixture is provided by conftest.py in this directory.
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _done(conn, tid):
    """Force a task to done regardless of claim state (test helper)."""
    kb.complete_task(conn, tid, result="ok")


# ===========================================================================
# Suite A — Linking & ownership tree (the foundation for group-by-ownership)
# ===========================================================================

def test_A1_create_with_parents_creates_edges(kanban_home):
    """[GREEN] create_task(parents=[r]) wires a real parent->child edge."""
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root", assignee="orchestrator")
        child = kb.create_task(conn, title="sub", assignee="worker-ops", parents=[root])
        assert root in kb.parent_ids(conn, child)
        assert child in kb.child_ids(conn, root)
    finally:
        conn.close()


def test_A2_child_waits_for_parents_then_promotes(kanban_home):
    """[GREEN] child stays 'todo' until all parents done, then recompute_ready -> ready.

    This is the fan-in mechanism. No 'blocked' status involved — proves the
    fan-in summarizer never needs block/unblock at all.
    """
    conn = kb.connect()
    try:
        p1 = kb.create_task(conn, title="p1", assignee="worker")
        p2 = kb.create_task(conn, title="p2", assignee="worker")
        fanin = kb.create_task(conn, title="fanin", assignee="worker-fast", parents=[p1, p2])
        assert kb.get_task(conn, fanin).status == "todo"

        _done(conn, p1)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, fanin).status == "todo"  # one parent still open

        # Completing the last parent promotes the child (complete_task runs
        # recompute_ready internally; an explicit call is a harmless no-op).
        _done(conn, p2)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, fanin).status == "ready"
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.root_of() not yet implemented")
def test_A3_root_of_walks_to_top_ancestor(kanban_home):
    """[TDD] root_of(task) returns the top human-initiated ancestor.

    Contract: a task with no parents is its own root.
    """
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root", assignee="orchestrator")
        sub = kb.create_task(conn, title="sub", assignee="worker", parents=[root])
        assert kb.root_of(conn, sub) == root
        assert kb.root_of(conn, root) == root
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.root_of() not yet implemented")
def test_A4_root_of_multilevel_tree(kanban_home):
    """[TDD] All nodes in a 3-level tree resolve to the same root.

    root -> orchestrator-subtask -> fan-in. Group-by-ownership depends on this.
    """
    conn = kb.connect()
    try:
        root = kb.create_task(conn, title="root", assignee="orchestrator")
        mid = kb.create_task(conn, title="mid", assignee="worker-research", parents=[root])
        leaf = kb.create_task(conn, title="leaf", assignee="worker-fast", parents=[mid])
        assert kb.root_of(conn, leaf) == root
        assert kb.root_of(conn, mid) == root
    finally:
        conn.close()


def test_A5_link_cycle_rejected(kanban_home):
    """[GREEN] link_tasks refuses to create a cycle."""
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="a", assignee="worker")
        b = kb.create_task(conn, title="b", assignee="worker", parents=[a])
        with pytest.raises(ValueError):
            kb.link_tasks(conn, parent_id=b, child_id=a)  # would cycle
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.prose_dep_violations() not yet implemented")
def test_A6_prose_dependency_is_flagged(kanban_home):
    """[TDD] A blocked task whose reason names a t_ id but has NO link is flagged.

    This is the t_5d8924a4 bug: worker wrote "waiting on t_db716212" as prose,
    set no real parents link, dep finished, task stranded forever.
    """
    conn = kb.connect()
    try:
        dep = kb.create_task(conn, title="dep", assignee="worker-research")
        waiter = kb.create_task(conn, title="waiter", assignee="worker-ops")
        kb.block_task(conn, waiter, reason=f"waiting on dependency task {dep} to complete")
        violations = kb.prose_dep_violations(conn, waiter)
        assert dep in violations  # mentioned in prose, never linked
    finally:
        conn.close()


# ===========================================================================
# Suite D — Auto-recovery (transient route-B failures, Python, no LLM)
# ===========================================================================

def test_D5_dependency_wait_via_link_auto_promotes(kanban_home):
    """[GREEN] A real parents link auto-resolves via recompute_ready — no block needed.

    The CORRECT pattern (vs Suite A6's prose anti-pattern).
    """
    conn = kb.connect()
    try:
        dep = kb.create_task(conn, title="dep", assignee="worker-research")
        waiter = kb.create_task(conn, title="waiter", assignee="worker-ops", parents=[dep])
        assert kb.get_task(conn, waiter).status == "todo"
        _done(conn, dep)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, waiter).status == "ready"
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.is_transient_failure() not yet implemented")
def test_D_classifier_transient_vs_permanent(kanban_home):
    """[TDD] is_transient_failure distinguishes recoverable from permanent errors."""
    assert kb.is_transient_failure("provider_api_failure: provider rate limit exceeded")
    assert kb.is_transient_failure("pid 63955 exited with code 1")
    assert kb.is_transient_failure("pid 53623 not alive")
    # permanent — must NOT be retried
    assert not kb.is_transient_failure("Unknown skill(s): kanban-worker, missing-skill")
    assert not kb.is_transient_failure("worker exited cleanly (rc=0) without calling complete")


@pytest.mark.xfail(strict=True, reason="TDD: kb.retry_transient_blocked() not yet implemented")
def test_D1_transient_after_cooldown_unblocks(kanban_home):
    """[TDD] Transient block + cooldown elapsed + under cap -> unblocked."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.block_task(conn, tid, reason="provider rate limit exceeded")
        now = int(time.time()) + 3600  # 1h later, past cooldown
        retried = kb.retry_transient_blocked(conn, cooldown_seconds=300, max_auto_retries=2, now=now)
        assert tid in retried
        assert kb.get_task(conn, tid).status in {"ready", "todo"}
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.retry_transient_blocked() not yet implemented")
def test_D2_transient_within_cooldown_stays_blocked(kanban_home):
    """[TDD] Transient block but cooldown not elapsed -> stays blocked."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.block_task(conn, tid, reason="provider rate limit exceeded")
        now = int(time.time()) + 10  # only 10s later
        retried = kb.retry_transient_blocked(conn, cooldown_seconds=300, max_auto_retries=2, now=now)
        assert tid not in retried
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.retry_transient_blocked() not yet implemented")
def test_D3_retry_cap_exhausted_stays_blocked(kanban_home):
    """[TDD] After max_auto_retries the task is left blocked for escalation."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        now = int(time.time())
        for i in range(3):
            kb.block_task(conn, tid, reason="provider rate limit exceeded")
            now += 3600
            kb.retry_transient_blocked(conn, cooldown_seconds=300, max_auto_retries=2, now=now)
        # 3rd attempt is past the cap of 2 -> remains blocked
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="TDD: kb.retry_transient_blocked() not yet implemented")
def test_D4_permanent_error_not_retried(kanban_home):
    """[TDD] A permanent (non-transient) block is never auto-unblocked."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.block_task(conn, tid, reason="Unknown skill(s): kanban-worker, missing-skill")
        now = int(time.time()) + 86400  # a day later
        retried = kb.retry_transient_blocked(conn, cooldown_seconds=300, max_auto_retries=2, now=now)
        assert tid not in retried
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


# ===========================================================================
# Suite E — Auto-archive garbage (body='' AND result='' AND age>1d AND blocked)
# ===========================================================================

GARBAGE_THRESHOLD_DAYS = 1


@pytest.mark.xfail(strict=True, reason="TDD: kb.is_garbage_task() not yet implemented")
def test_E1_empty_body_no_result_old_is_garbage(kanban_home):
    """[TDD] body='' AND result='' AND blocked AND age>1d -> garbage."""
    now = int(time.time())
    assert kb.is_garbage_task(
        status="blocked", body_len=0, has_result=False,
        blocked_at=now - 2 * 86400, now=now, age_threshold_days=GARBAGE_THRESHOLD_DAYS,
    ) is True


@pytest.mark.xfail(strict=True, reason="TDD: kb.is_garbage_task() not yet implemented")
def test_E2_nonempty_body_never_garbage(kanban_home):
    """[TDD] A real body is NEVER garbage, even old + blocked (the safety guard)."""
    now = int(time.time())
    assert kb.is_garbage_task(
        status="blocked", body_len=500, has_result=False,
        blocked_at=now - 30 * 86400, now=now, age_threshold_days=GARBAGE_THRESHOLD_DAYS,
    ) is False


@pytest.mark.xfail(strict=True, reason="TDD: kb.is_garbage_task() not yet implemented")
def test_E3_fresh_empty_stub_not_archived(kanban_home):
    """[TDD] Empty body but younger than threshold -> spared (give it time to get a spec)."""
    now = int(time.time())
    assert kb.is_garbage_task(
        status="blocked", body_len=0, has_result=False,
        blocked_at=now - 3600, now=now, age_threshold_days=GARBAGE_THRESHOLD_DAYS,
    ) is False


@pytest.mark.xfail(strict=True, reason="TDD: kb.is_garbage_task() not yet implemented")
def test_E4_empty_body_with_result_not_garbage(kanban_home):
    """[TDD] Empty body but produced output -> not garbage."""
    now = int(time.time())
    assert kb.is_garbage_task(
        status="blocked", body_len=0, has_result=True,
        blocked_at=now - 30 * 86400, now=now, age_threshold_days=GARBAGE_THRESHOLD_DAYS,
    ) is False


@pytest.mark.xfail(strict=True, reason="TDD: kb.is_garbage_task() not yet implemented")
def test_E5_golden_dataset_matches_labels():
    """[TDD] ANCHOR: the rule reproduces the human labels on the real board snapshot.

    Loads the 58 real blocked tasks (25 garbage / 33 real, the 0-vs-500
    body-length cliff) and asserts the predicate flags every garbage task and
    zero real tasks. `now` is set 2 days past the snapshot so the age guard is
    satisfied for all rows — this isolates the body/result discriminator
    (the age guard itself is covered by E3).
    """
    snap = json.loads((FIXTURES / "blocked_board_snapshot.json").read_text())
    now = snap["snapshot_unixtime"] + 2 * 86400
    threshold = snap["garbage_age_threshold_days"]
    wrong = []
    for t in snap["tasks"]:
        verdict = kb.is_garbage_task(
            status="blocked", body_len=t["body_len"], has_result=t["has_result"],
            blocked_at=t["blocked_at"], now=now, age_threshold_days=threshold,
        )
        expected = (t["label"] == "garbage")
        if verdict != expected:
            wrong.append((t["id"], t["title"], expected, verdict))
    assert not wrong, f"{len(wrong)} misclassified: {wrong[:5]}"


@pytest.mark.xfail(strict=True, reason="TDD: kb.archive_garbage() not yet implemented")
def test_E_archive_garbage_action(kanban_home):
    """[TDD] archive_garbage archives only matching tasks, returns their ids."""
    conn = kb.connect()
    try:
        # garbage: empty body, blocked, old
        junk = kb.create_task(conn, title="crashy", assignee="worker")
        kb.block_task(conn, junk, reason="no body/spec")
        # real: has body, blocked, old
        real = kb.create_task(conn, title="real task", body="x" * 600, assignee="worker")
        kb.block_task(conn, real, reason="needs OAuth")

        now = int(time.time()) + 2 * 86400
        archived = kb.archive_garbage(conn, age_threshold_days=1, now=now)
        assert junk in archived
        assert real not in archived
        assert kb.get_task(conn, junk).status == "archived"
        assert kb.get_task(conn, real).status == "blocked"
    finally:
        conn.close()
