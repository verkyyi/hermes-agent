"""Local tests extracted from /Users/verkyyi/.claude/jobs/64cd9d39/head_codeexec.py.

Kept in tests/local/ so upstream merges don't conflict on local test
additions. Methods preserved verbatim inside a local class; upstream
helpers/fixtures imported from the original module.
"""
from __future__ import annotations

import pytest
import json
import os
import sys
import time
import threading
import unittest
from unittest.mock import patch, MagicMock
from tools.code_execution_tool import (
    SANDBOX_ALLOWED_TOOLS,
    execute_code,
    generate_hermes_tools_module,
    check_sandbox_requirements,
    build_execute_code_schema,
    EXECUTE_CODE_SCHEMA,
    _TOOL_DOC_LINES,
    _execute_remote,
)

from tests.tools.test_code_execution import (  # noqa: F401
    _mock_handle_function_call,
)


class TestCodeExecRequestIdLocal(unittest.TestCase):
    def test_nested_tool_call_preserves_request_id(self):
        """execute_code RPC calls should keep the foreground request id."""
        code = """
from hermes_tools import terminal
terminal("echo hello")
"""
        seen = []

        def capture(function_name, function_args, task_id=None, user_task=None, **kwargs):
            seen.append((function_name, task_id, kwargs.get("request_id")))
            return _mock_handle_function_call(
                function_name, function_args, task_id=task_id,
                user_task=user_task, **kwargs
            )

        with patch("model_tools.handle_function_call", side_effect=capture):
            result = execute_code(
                code=code,
                task_id="test-task",
                request_id="req-123",
                enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
            )

        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success", parsed)
        self.assertEqual(seen, [("terminal", "test-task", "req-123")])
