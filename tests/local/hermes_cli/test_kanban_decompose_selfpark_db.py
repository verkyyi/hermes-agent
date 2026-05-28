"""Local tests: self-park path of ``kb.decompose_triage_task`` (allow_running).

Path A of the decompose-anchor design — an orchestrator decomposing its own
in-flight (``running``) task, parking it as the fan-in anchor. Generalizes the
upstream triage-only decompose; the upstream behavior (triage-only by default,
returns None otherwise) is left untouched and covered by
``tests/hermes_cli/test_kanban_decompose_db.py``.

See docs/plans/2026-05-28-kanban-wake-origin-session.md.
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


CHILDREN = [
    {"title": "lane A", "body": "do A", "assignee": "worker-a", "parents": []},
    {"title": "lane B", "body": "do B", "assignee": "worker-b", "parents": []},
    {"title": "synthesize", "body": "combine A+B", "assignee": "worker-c", "parents": [0, 1]},
]


def _make_running(conn, title="orchestrator task"):
    """Create a task and drive it to ``running`` with an open run + live pid."""
    tid = kb.create_task(conn, title=title, assignee="orchestrator")
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    # claim_task opens the run + sets claim_lock; the spawner (not claim) sets
    # the worker pid — simulate a live worker so we can assert it's cleared.
    conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", (tid,))
    conn.commit()
    return tid


def test_self_decompose_parks_running_anchor(kanban_home):
    with kb.connect() as conn:
        tid = _make_running(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert run_id is not None

        child_ids = kb.decompose_triage_task(
            conn, tid,
            root_assignee="orchestrator",
            children=CHILDREN,
            author="orchestrator",
            allow_running=True,
        )
        assert child_ids is not None and len(child_ids) == 3

        root = kb.get_task(conn, tid)
        # Parked, NOT completed; claim + pid + run pointer all cleared so the
        # worker's clean exit is not treated as a crash/protocol violation.
        assert root.status == "todo"
        assert root.assignee == "orchestrator"
        assert root.current_run_id is None
        assert root.worker_pid is None
        assert root.claim_lock is None

        # The prior run was closed with a non-failure outcome.
        run_row = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        assert run_row["ended_at"] is not None
        assert run_row["outcome"] == "decomposed"

        # Parallel lanes promoted; synthesis waits on both.
        assert kb.get_task(conn, child_ids[0]).status == "ready"
        assert kb.get_task(conn, child_ids[1]).status == "ready"
        assert kb.get_task(conn, child_ids[2]).status == "todo"


def test_self_decompose_requires_allow_running_flag(kanban_home):
    """Default (allow_running=False) preserves upstream's triage-only guard."""
    with kb.connect() as conn:
        tid = _make_running(conn)
        result = kb.decompose_triage_task(
            conn, tid,
            root_assignee="orchestrator",
            children=CHILDREN,
            author="orchestrator",
        )
        assert result is None
        # Untouched: still running, run still open.
        root = kb.get_task(conn, tid)
        assert root.status == "running"
        assert root.current_run_id is not None


def test_self_decomposed_anchor_repromotes_when_children_done(kanban_home):
    """Claim 1 in the self-park context: anchor re-promotes once ALL children done."""
    with kb.connect() as conn:
        tid = _make_running(conn)
        child_ids = kb.decompose_triage_task(
            conn, tid,
            root_assignee="orchestrator",
            children=CHILDREN,
            author="orchestrator",
            allow_running=True,
        )
        assert child_ids is not None
        assert kb.get_task(conn, tid).status == "todo"

        # Finish the two parallel lanes → synthesis promotes, anchor still waits
        # (it is a child of every child, including the synthesis lane).
        kb.complete_task(conn, child_ids[0], summary="A done")
        kb.complete_task(conn, child_ids[1], summary="B done")
        assert kb.get_task(conn, child_ids[2]).status == "ready"
        assert kb.get_task(conn, tid).status == "todo"

        # Finish synthesis → all children done → anchor re-promotes to ready.
        kb.complete_task(conn, child_ids[2], summary="combined")
        assert kb.get_task(conn, tid).status == "ready"
