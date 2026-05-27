"""Local tests extracted from /Users/verkyyi/.claude/jobs/64cd9d39/head_mtools.py.

Kept in tests/local/ so upstream merges don't conflict on local test
additions. Methods preserved verbatim inside a local class; upstream
helpers/fixtures imported from the original module.
"""
from __future__ import annotations

import json
from unittest.mock import ANY, call, patch
import pytest
from model_tools import (
    handle_function_call,
    get_all_tool_names,
    get_toolset_for_tool,
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
)

class TestModelToolsRequestIdLocal:
    def test_request_id_is_accepted_and_forwarded_to_registry_and_hooks(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}') as mock_dispatch,
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            result = handle_function_call(
                "kanban_create",
                {"title": "child task"},
                task_id="task-1",
                tool_call_id="call-1",
                session_id="session-1",
                request_id="req-123",
                platform="telegram",
            )

        assert result == '{"ok":true}'
        assert mock_dispatch.call_args.kwargs["request_id"] == "req-123"
        assert mock_dispatch.call_args.kwargs["session_id"] == "session-1"
        hook_kwargs = {
            c.args[0]: c.kwargs for c in mock_invoke_hook.call_args_list
        }
        assert hook_kwargs["post_tool_call"]["request_id"] == "req-123"
        assert hook_kwargs["transform_tool_result"]["request_id"] == "req-123"

    def test_request_id_reaches_representative_real_tool_handlers(self, tmp_path, monkeypatch):
        """Regression: metadata kwargs must not break real handlers.

        This exercises representative file, terminal, and Kanban tools through
        the normal dispatcher with request_id present. The historical failure
        was ``handle_function_call() got an unexpected keyword argument
        'request_id'`` or a handler rejecting forwarded kwargs.
        """
        from hermes_cli import kanban_db as kb

        smoke_file = tmp_path / "smoke.txt"
        smoke_file.write_text("hello\n", encoding="utf-8")
        db_path = tmp_path / "kanban.db"
        workspace_root = tmp_path / "workspaces"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(workspace_root))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "request-id-smoke")
        monkeypatch.setenv("HERMES_PROFILE", "request-id-smoke")

        conn = kb.connect(db_path=db_path)
        task_id = kb.create_task(
            conn,
            title="request_id smoke",
            assignee="request-id-smoke",
            created_by="request-id-smoke",
        )
        claimed = kb.claim_task(conn, task_id, claimer="request-id-smoke")
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

        calls = [
            ("read_file", {"path": str(smoke_file), "limit": 1}),
            ("terminal", {"command": "printf request-id-ok", "timeout": 30, "workdir": str(tmp_path)}),
            ("kanban_show", {"task_id": task_id}),
            ("kanban_complete", {"summary": "request_id smoke passed"}),
        ]
        for tool_name, args in calls:
            parsed = json.loads(
                handle_function_call(
                    tool_name,
                    args,
                    task_id=task_id,
                    session_id="session-request-id-smoke",
                    request_id="req-real-handlers",
                )
            )
            assert not parsed.get("error"), (tool_name, parsed)

        assert kb.get_task(conn, task_id).status == "done"
