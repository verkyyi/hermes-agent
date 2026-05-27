"""Local tests extracted from /Users/verkyyi/.claude/jobs/64cd9d39/head_kdb.py.

Kept in the tests/local/ tree so upstream merges don't conflict on local
test additions. Upstream helpers/fixtures are imported from the original
module rather than duplicated.
"""
from __future__ import annotations

from __future__ import annotations
import concurrent.futures
import os
import time
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

# Upstream helpers/fixtures reused by the extracted tests.
from tests.hermes_cli.test_kanban_db import (  # noqa: F401
    kanban_home,
)


def test_notify_sub_request_id_roundtrips(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="correlated", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="123",
            thread_id="456",
            user_id="u1",
            notification_mode="synthesize",
            request_id="req_abc123",
        )
        subs = kb.list_notify_subs(conn, task_id)
        attrs = kb._task_correlation_attrs(conn, task_id)

    assert len(subs) == 1
    assert subs[0]["request_id"] == "req_abc123"
    assert subs[0]["notification_mode"] == "synthesize"
    assert attrs == {
        "task_id": task_id,
        "request_id": "req_abc123",
        "notification_mode": "synthesize",
        "platform": "telegram",
    }


def test_dispatch_and_completion_emit_segmented_telemetry(kanban_home, all_assignees_spawnable, monkeypatch):
    recorded = []
    monkeypatch.setattr(kb, "_record_kanban_span", lambda name, **kw: recorded.append((name, kw)))

    def fake_spawn(task, workspace):
        return 12345

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="telemetry", assignee="worker")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res.spawned and res.spawned[0][0] == tid
        assert kb.complete_task(conn, tid, summary="done") is True

    names = [name for name, _ in recorded]
    assert "queue.wait" in names
    assert "worker.spawn" in names
    assert "worker.run" in names
    for _name, kwargs in recorded:
        attrs = kwargs.get("attributes") or {}
        # Safe correlation metadata only: request/task correlation is OK, but no raw message/body content.
        assert attrs.get("task_id") == tid
        assert "message" not in attrs
        assert "body" not in attrs


def test_heartbeat_worker_extends_claim_for_owned_current_run(kanban_home):
    """A run-scoped heartbeat should extend the active claim TTL.

    This covers callers that use the DB API directly (not only the
    kanban_heartbeat tool wrapper) and prevents long-running workers from
    being reclaimed while they are actively heartbeating.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="long", assignee="alice")
        claimed = kb.claim_task(conn, t, claimer="host:owned", ttl_seconds=60)
        run_id = claimed.current_run_id
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (1, t))
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (1, run_id))

        assert kb.heartbeat_worker(conn, t, note="still working", expected_run_id=run_id)

        task = kb.get_task(conn, t)
        run = kb.latest_run(conn, t)
        assert task.claim_expires >= int(time.time()) + kb.DEFAULT_CLAIM_TTL_SECONDS - 5
        assert run.claim_expires == task.claim_expires
        assert kb.release_stale_claims(conn) == 0
        assert kb.get_task(conn, t).status == "running"


def test_stale_heartbeat_worker_does_not_extend_foreign_run(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="stale", assignee="alice")
        first = kb.claim_task(conn, t, claimer="host:first", ttl_seconds=60)
        first_run_id = first.current_run_id
        kb.reclaim_task(conn, t, reason="simulate reclaim")
        second = kb.claim_task(conn, t, claimer="host:second", ttl_seconds=60)
        second_run_id = second.current_run_id
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (1, t))
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (1, second_run_id))

        assert not kb.heartbeat_worker(conn, t, note="old worker", expected_run_id=first_run_id)

        task = kb.get_task(conn, t)
        run = kb.latest_run(conn, t)
        assert task.claim_expires == 1
        assert run.claim_expires == 1


def test_completion_rejection_context_identifies_stale_run(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="race", assignee="alice")
        first = kb.claim_task(conn, t, claimer="host:first")
        first_run_id = first.current_run_id
        kb.reclaim_task(conn, t, reason="simulate stale worker")
        second = kb.claim_task(conn, t, claimer="host:second")
        ctx = kb.completion_rejection_context(conn, t, expected_run_id=first_run_id)

        assert ctx["task_id"] == t
        assert ctx["task_status"] == "running"
        assert ctx["current_run_id"] == second.current_run_id
        assert ctx["expected_run_id"] == first_run_id
        assert ctx["reason"] == "stale_run"
        assert "recovery_guidance" in ctx


def test_recover_complete_blocked_task_preserves_audit_history(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="finished-but-blocked", assignee="alice")
        kb.claim_task(conn, t, claimer="host:worker")
        kb.block_task(conn, t, reason="gave_up after diagnostics")

        ok = kb.recover_complete_task(
            conn,
            t,
            summary="verified handoff from comment",
            metadata={"source": "operator_verified_comment"},
            recovered_by="operator",
            reason="work completed after worker lifecycle failure",
        )

        assert ok
        task = kb.get_task(conn, t)
        assert task.status == "done"
        run = kb.latest_run(conn, t)
        assert run.outcome == "recovered_completed"
        assert run.summary == "verified handoff from comment"
        assert run.metadata["source"] == "operator_verified_comment"
        events = kb.list_events(conn, t)
        assert events[-1].kind == "recovered_completed"
        assert events[-1].payload["recovered_by"] == "operator"


def test_spawn_failure_payload_includes_log_tail(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="bad skill", assignee="alice")
        kb.claim_task(conn, t, claimer="host:worker")
        log_path = kb.worker_log_path(t)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("Error: Unknown skill(s): missing-skill\n", encoding="utf-8")

        kb._record_spawn_failure(
            conn,
            t,
            "pid 123 not alive",
            failure_limit=1,
            event_payload_extra=kb.failure_diagnostics(conn, t),
        )

        run = kb.latest_run(conn, t)
        assert "Unknown skill(s): missing-skill" in run.metadata["worker_log_tail"]
        gave_up = [e for e in kb.list_events(conn, t) if e.kind == "gave_up"][-1]
        assert "Unknown skill(s): missing-skill" in gave_up.payload["worker_log_tail"]


def test_dispatch_preflights_unknown_forced_skill_before_spawn(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="needs missing skill",
            assignee="default",
            skills=["missing-kanban-skill"],
        )
        called = {"spawn": False}

        def spawn_fn(_task, _workspace):
            called["spawn"] = True
            return 123

        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, failure_limit=1)

        assert not called["spawn"]
        assert result.auto_blocked == [t]
        task = kb.get_task(conn, t)
        assert task.status == "blocked"
        run = kb.latest_run(conn, t)
        assert run.outcome == "gave_up"
        assert "missing-kanban-skill" in run.error
        assert run.metadata["preflight"] == "skills"
        assert "missing-kanban-skill" in run.metadata["missing_skills"]
