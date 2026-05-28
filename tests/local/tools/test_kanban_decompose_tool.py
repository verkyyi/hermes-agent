"""Local tests: the ``kanban_decompose`` tool handler (Path A self-park).

The orchestrator calls this on its own dispatched task (HERMES_KANBAN_TASK) to
fan out children and self-park as the fan-in anchor.
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


def _running_task(title="orchestrate"):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="orchestrator")
        kb.recompute_ready(conn)
        kb.claim_task(conn, tid)
    return tid


def test_handle_decompose_self_parks_current_task(kanban_home, monkeypatch):
    tid = _running_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")

    out = kt._handle_decompose({
        "children": [
            {"title": "A", "assignee": "worker-a"},
            {"title": "B", "assignee": "worker-b"},
            {"title": "sum", "assignee": "worker-c", "parents": [0, 1]},
        ],
    })
    payload = json.loads(out)
    assert payload.get("ok") is True
    assert payload.get("parked") is True
    assert len(payload.get("children", [])) == 3

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
    assert root.status == "todo"
    assert root.current_run_id is None


def test_handle_decompose_requires_kanban_task_env(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = kt._handle_decompose({"children": [{"title": "x", "assignee": "y"}]})
    assert "error" in json.loads(out)


def test_handle_decompose_requires_child_assignee(kanban_home, monkeypatch):
    tid = _running_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    out = kt._handle_decompose({"children": [{"title": "no assignee"}]})
    assert "error" in json.loads(out)


def test_handle_decompose_rejects_empty_children(kanban_home, monkeypatch):
    tid = _running_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    out = kt._handle_decompose({"children": []})
    assert "error" in json.loads(out)
