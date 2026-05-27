"""Orchestrator benchmark — Suite F: full lifecycle, end to end.

Drives the whole target flow through real dispatch_once ticks with a scripted
spawn_fn that stands in for LLM workers (deterministic, no LLM):

    root request
      -> orchestrator fans out 2 silent sub-tasks (parents=[root]) + 1 fan-in
      -> sub A blocks (waiting on human), sub B completes
      -> group-by-ownership surfaces A under its root
      -> human resolves -> A unblocks -> completes
      -> fan-in promotes (all parents done) -> completes
      -> tree fully done, exactly one fan-in result

Marked xfail(strict=True): depends on kb.blocked_grouped_by_root() (Suite C),
which is not built yet. Flips to a failure once implemented — the "E2E done"
signal.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb

# kanban_home + all_assignees_spawnable fixtures come from conftest.py here.


@pytest.mark.xfail(strict=True, reason="TDD: depends on kb.blocked_grouped_by_root() (Suite C)")
def test_F1_full_lifecycle_block_resolve_fanin(kanban_home, all_assignees_spawnable):
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn, title="research vector DBs and update notes",
            body="x" * 600, assignee="orchestrator",
        )

        state = {"ids": {}, "a_attempts": 0}

        def spawn(task, ws):
            """Scripted worker. Performs its action synchronously, returns no pid."""
            title = task.title
            if task.assignee == "orchestrator":
                # Fan out: two silent sub-tasks linked to the root, plus a
                # fan-in that waits on both. Then self-complete.
                a = kb.create_task(conn, title="compare DBs", body="x" * 600,
                                   assignee="worker-research", parents=[root])
                b = kb.create_task(conn, title="update notes", body="x" * 600,
                                   assignee="worker-ops", parents=[root])
                fanin = kb.create_task(conn, title="consolidate", body="x" * 600,
                                       assignee="worker-fast", parents=[a, b])
                state["ids"] = {"a": a, "b": b, "fanin": fanin}
                kb.complete_task(conn, root, result="routed to research+ops, fan-in queued")
            elif title == "compare DBs":
                state["a_attempts"] += 1
                if state["a_attempts"] == 1:
                    kb.block_task(conn, task.id, reason="need dataset access credentials")
                else:
                    kb.complete_task(conn, task.id, result="pinecone vs weaviate vs qdrant")
            elif title == "update notes":
                kb.complete_task(conn, task.id, result="notes updated")
            elif title == "consolidate":
                kb.complete_task(conn, task.id, result="final comparison report")
            return None

        # Tick 1: claim+spawn root -> fan-out created, root done.
        kb.dispatch_once(conn, spawn_fn=spawn)
        ids = state["ids"]
        assert kb.get_task(conn, root).status == "done"

        # Tick 2: A and B promoted (parent root done) -> spawned. A blocks, B completes.
        kb.dispatch_once(conn, spawn_fn=spawn)
        assert kb.get_task(conn, ids["a"]).status == "blocked"
        assert kb.get_task(conn, ids["b"]).status == "done"
        assert kb.get_task(conn, ids["fanin"]).status == "todo"  # still waiting on A

        # Group-by-ownership: A surfaces under its root, alone.
        groups = kb.blocked_grouped_by_root(conn)
        assert root in groups
        assert {t.id for t in groups[root]} == {ids["a"]}

        # Human resolves the blocker.
        assert kb.unblock_task(conn, ids["a"]) is True

        # Tick 3: A spawned again -> completes. fan-in now has all parents done.
        kb.dispatch_once(conn, spawn_fn=spawn)
        assert kb.get_task(conn, ids["a"]).status == "done"

        # Tick 4: fan-in promoted -> spawned -> completes.
        kb.dispatch_once(conn, spawn_fn=spawn)
        assert kb.get_task(conn, ids["fanin"]).status == "done"

        # Whole tree done; the fan-in carries the single consolidated result.
        for tid in (root, ids["a"], ids["b"], ids["fanin"]):
            assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, ids["fanin"]).result == "final comparison report"
    finally:
        conn.close()
