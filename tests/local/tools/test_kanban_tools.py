"""Local tests extracted from /Users/verkyyi/.claude/jobs/64cd9d39/head_ktools.py.

Kept in the tests/local/ tree so upstream merges don't conflict on local
test additions. Upstream helpers/fixtures are imported from the original
module rather than duplicated.
"""
from __future__ import annotations

from __future__ import annotations
import json
import os
import pytest

# Upstream helpers/fixtures reused by the extracted tests.
from tests.tools.test_kanban_tools import (  # noqa: F401
    worker_env,
)


def test_create_worker_root_task_inherits_current_origin_subscription(worker_env):
    """Parentless worker-created follow-up tasks keep the interactive origin."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        kb.add_notify_sub(
            conn,
            task_id=worker_env,
            platform="telegram",
            chat_id="chat-456",
            thread_id="thread-789",
            user_id="user-1",
            notification_mode="synthesize",
            origin_session_id="origin-session",
            origin_profile="default",
            origin_context="please recover this user-visible task",
        )

    out = kt._handle_create({
        "title": "root recovery follow-up",
        "assignee": "worker",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["notification_subscription"] == {
        "platform": "telegram",
        "chat_id": "chat-456",
        "thread_id": "thread-789",
        "notification_mode": "synthesize",
        "request_id": None,
        "inherited_from_task": worker_env,
    }

    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, d["task_id"])
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat-456"
    assert subs[0]["thread_id"] == "thread-789"
    assert subs[0]["user_id"] == "user-1"
    assert subs[0]["notification_mode"] == "synthesize"
    assert subs[0]["origin_session_id"] == "origin-session"
    assert subs[0]["origin_profile"] == "default"
    assert subs[0]["origin_context"] == "please recover this user-visible task"


def test_create_worker_child_does_not_inherit_current_origin_subscription(worker_env):
    """Normal worker fan-out linked by parent remains silent."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        kb.add_notify_sub(
            conn,
            task_id=worker_env,
            platform="telegram",
            chat_id="chat-456",
            notification_mode="synthesize",
        )

    out = kt._handle_create({
        "title": "internal child",
        "assignee": "worker",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["notification_subscription"] is None

    with kb.connect() as conn:
        assert kb.list_notify_subs(conn, d["task_id"]) == []


def test_create_worker_root_task_respects_silent_notification_mode(worker_env):
    """Workers can still explicitly suppress inherited origin delivery."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        kb.add_notify_sub(
            conn,
            task_id=worker_env,
            platform="telegram",
            chat_id="chat-456",
            notification_mode="synthesize",
        )

    out = kt._handle_create({
        "title": "silent root follow-up",
        "assignee": "worker",
        "notification_mode": "silent",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["notification_subscription"] is None

    with kb.connect() as conn:
        assert kb.list_notify_subs(conn, d["task_id"]) == []


def test_create_auto_subscribes_cli_origin(monkeypatch, tmp_path):
    """Model-tool-created tasks subscribe the initiating CLI session."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    import model_tools
    out = model_tools.handle_function_call(
        "kanban_create",
        {"title": "agent-created", "assignee": "worker"},
        task_id="tool-session",
        platform="cli",
        session_id="session-123",
        skip_pre_tool_call_hook=True,
    )
    d = json.loads(out)
    assert d["ok"] is True
    assert d["notification_subscription"] == {
        "platform": "cli",
        "chat_id": "session-123",
        "thread_id": "",
        "notification_mode": "direct",
        "request_id": None,
    }

    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, d["task_id"])
        assert len(subs) == 1
        assert subs[0]["platform"] == "cli"
        assert subs[0]["chat_id"] == "session-123"
        assert subs[0]["thread_id"] == ""

        kb.complete_task(conn, d["task_id"], summary="done by synthetic worker")
        new_cursor, events = kb.unseen_events_for_sub(
            conn,
            task_id=d["task_id"],
            platform="cli",
            chat_id="session-123",
            kinds=("completed", "blocked", "gave_up", "crashed", "timed_out"),
        )
    assert new_cursor > 0
    assert [event.kind for event in events] == ["completed"]
    assert events[0].payload["summary"] == "done by synthetic worker"


def test_create_auto_subscribes_gateway_origin_with_chat_id(monkeypatch, tmp_path):
    """Gateway-origin tool calls subscribe only when the routable chat id is present."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    from tools import kanban_tools as kt
    out = kt._handle_create(
        {"title": "gateway-agent-created", "assignee": "worker"},
        platform="telegram",
        session_id="not-routable-session",
        chat_id="chat-456",
        thread_id="thread-789",
        user_id="user-1",
        user_task="please run this with context",
    )
    d = json.loads(out)
    assert d["ok"] is True
    assert d["notification_subscription"] == {
        "platform": "telegram",
        "chat_id": "chat-456",
        "thread_id": "thread-789",
        "notification_mode": "synthesize",
        "request_id": None,
    }
    assert d["user_facing_status"] == "I’ll look into it and report back here."

    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, d["task_id"])
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat-456"
    assert subs[0]["thread_id"] == "thread-789"
    assert subs[0]["user_id"] == "user-1"
    assert subs[0]["notification_mode"] == "synthesize"
    assert subs[0]["origin_session_id"] == "not-routable-session"
    assert subs[0]["origin_profile"] == "orchestrator"
    assert subs[0]["origin_context"] == "please run this with context"


def test_create_does_not_subscribe_gateway_origin_without_chat_id(monkeypatch, tmp_path):
    """A gateway session id alone is not a routable notification endpoint."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    from tools import kanban_tools as kt
    out = kt._handle_create(
        {"title": "gateway-no-chat", "assignee": "worker"},
        platform="telegram",
        session_id="not-routable-session",
    )
    d = json.loads(out)
    assert d["ok"] is True
    assert d["notification_subscription"] is None

    with kb.connect() as conn:
        assert kb.list_notify_subs(conn, d["task_id"]) == []


def test_complete_stale_run_error_is_structured(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        first_run_id = kb.latest_run(conn, worker_env).id
        kb.reclaim_task(conn, worker_env, reason="simulate reclaim")
        kb.claim_task(conn, worker_env, claimer="host:new")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(first_run_id))

    out = kt._handle_complete({"summary": "old worker done"})
    err = json.loads(out)
    assert err.get("error")
    assert "stale_run" in err["error"]
    assert f"expected_run_id={first_run_id}" in err["error"]
    assert "current_run_id=" in err["error"]
    assert "recovery" in err["error"].lower()
