"""Local tests extracted from /Users/verkyyi/.claude/jobs/64cd9d39/head_baseenv.py.

Kept in the tests/local/ tree so upstream merges don't conflict on local
test additions. Upstream helpers/fixtures are imported from the original
module rather than duplicated.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock
from tools.environments.base import BaseEnvironment, _auto_kanban_heartbeat_if_due, _cwd_marker

class TestAutoKanbanHeartbeat:
    def test_skips_before_sixty_seconds(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
        calls = []

        import hermes_cli.kanban_db as kb
        monkeypatch.setattr(kb, "connect", lambda: calls.append("connect"))

        state = {"start": 0.0, "last_touch": 0.0}
        _auto_kanban_heartbeat_if_due(state, "terminal command running", 59.0)

        assert calls == []
        assert "last_kanban_heartbeat" not in state

    def test_records_worker_heartbeat_every_sixty_seconds_and_redacts_label(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:pid")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")

        import hermes_cli.kanban_db as kb

        calls = []

        class FakeConn:
            def close(self):
                calls.append(("close",))

        monkeypatch.setattr(kb, "connect", lambda: FakeConn())
        monkeypatch.setattr(
            kb,
            "heartbeat_claim",
            lambda conn, task_id, claimer=None: calls.append(("claim", task_id, claimer)),
        )
        monkeypatch.setattr(
            kb,
            "heartbeat_worker",
            lambda conn, task_id, note=None, expected_run_id=None: calls.append(
                ("worker", task_id, note, expected_run_id)
            ),
        )

        state = {"start": 0.0, "last_touch": 0.0}
        _auto_kanban_heartbeat_if_due(
            state,
            "test command running: pytest OPENAI_API_KEY=sk-1234567890abcdef",
            61.0,
        )
        # Cadence gate: another call at +119s is still inside the 60s interval.
        _auto_kanban_heartbeat_if_due(state, "test command running: pytest", 119.0)
        _auto_kanban_heartbeat_if_due(state, "test command running: pytest", 122.0)

        worker_calls = [call for call in calls if call[0] == "worker"]
        assert len(worker_calls) == 2
        assert worker_calls[0][1] == "t_test"
        assert worker_calls[0][3] == 42
        assert "sk-1234567890abcdef" not in worker_calls[0][2]
        assert "test command running" in worker_calls[0][2]
        assert ("claim", "t_test", "host:pid") in calls
