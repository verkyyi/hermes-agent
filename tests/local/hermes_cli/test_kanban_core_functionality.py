"""Local tests extracted from /Users/verkyyi/.claude/jobs/64cd9d39/head_kcore.py.

Kept in the tests/local/ tree so upstream merges don't conflict on local
test additions. Upstream helpers/fixtures are imported from the original
module rather than duplicated.
"""
from __future__ import annotations

from __future__ import annotations
import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash

# Upstream helpers/fixtures reused by the extracted tests.
from tests.hermes_cli.test_kanban_core_functionality import (  # noqa: F401
    kanban_home,
)


def _write_test_skill(root: Path, name: str) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: test skill {name}\n"
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_dispatch_preflight_unknown_forced_skill_blocks_without_spawn(
    kanban_home, all_assignees_spawnable
):
    """Unknown forced skills are caught before spawning a worker subprocess."""
    from hermes_cli.profiles import get_profile_dir
    worker_home = get_profile_dir("worker")
    _write_test_skill(worker_home, "kanban-worker")
    _write_test_skill(kanban_home, "kanban-worker")
    spawned = []

    def fake_spawn(task, ws):
        spawned.append(task.id)
        return 123

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="bad forced skill",
            assignee="worker",
            skills=["does-not-exist"],
            priority=999999,
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

        assert tid not in spawned
        assert tid in result.auto_blocked
        assert task.status == "blocked"
        assert "Unknown skill(s): does-not-exist" in task.last_failure_error
        # A missing forced skill is a *permanent* failure: it must emit a
        # sticky ``blocked`` event (not ``gave_up``) so the dispatcher cannot
        # auto-revive it into an infinite respawn loop.
        block_events = [e for e in events if e.kind == "blocked"]
        assert block_events
        payload = block_events[-1].payload
        assert payload["permanent"] is True
        assert payload["preflight"] == "skills"
        assert payload["profile"] == "worker"
        assert payload["missing_skills"] == ["does-not-exist"]
        assert not [e for e in events if e.kind == "gave_up"], (
            "permanent preflight failure must be sticky-blocked, not gave_up"
        )

        # Regression for the respawn loop: a sticky permanent-failure block
        # must survive recompute_ready and never be re-spawned.
        assert kb._has_sticky_block(conn, tid)
        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, tid).status == "blocked"
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert tid not in spawned, "sticky-blocked task must not be re-spawned"
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_forced_skill_can_resolve_from_default_profile_for_named_worker(
    kanban_home, monkeypatch
):
    """A task-forced skill in the default profile can load for a named worker."""
    from hermes_cli.profiles import get_profile_dir
    worker_home = get_profile_dir("worker")
    worker_home.mkdir(parents=True, exist_ok=True)
    kanban_worker_dir = _write_test_skill(kanban_home, "kanban-worker")
    shared_skill_dir = _write_test_skill(kanban_home, "shared-only")
    captured = {}

    class FakeProc:
        pid = 456

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="default skill fallback",
            assignee="worker",
            skills=["shared-only"],
        )
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task)
        preflight = kb._preflight_task_skills(task)
        pid = kb._default_spawn(task, str(workspace))
    finally:
        conn.close()

    assert preflight is None
    assert pid == 456
    cmd = captured["cmd"]
    skill_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--skills"]
    assert str(kanban_worker_dir) in skill_args
    assert str(shared_skill_dir) in skill_args


def test_forced_skill_resolution_ignores_cached_default_skills_dir(
    kanban_home, monkeypatch
):
    """Preflight must work even if skills_tool was imported under default HOME.

    The gateway imports skills tooling under the default profile before the
    dispatcher probes worker profiles.  This regression catches the cached
    SKILLS_DIR bug that made preflight think a named worker could load a
    default-profile-only skill by name, causing the child CLI to crash with
    `Unknown skill(s)` instead of receiving an absolute fallback path.
    """
    from hermes_cli.profiles import get_profile_dir
    import tools.skills_tool as skills_tool

    worker_home = get_profile_dir("worker")
    worker_home.mkdir(parents=True, exist_ok=True)
    shared_skill_dir = _write_test_skill(kanban_home, "shared-only")
    kanban_worker_dir = _write_test_skill(kanban_home, "kanban-worker")

    # Simulate gateway/default-profile import-time cache.
    skills_tool.HERMES_HOME = kanban_home
    skills_tool.SKILLS_DIR = kanban_home / "skills"

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="cached skills dir fallback",
            assignee="worker",
            skills=["shared-only"],
        )
        task = kb.get_task(conn, tid)
        resolved_args, loaded, missing, resolved_map = kb._resolve_forced_skill_args(
            "worker",
            kb._forced_skill_identifiers(task),
            task_id=task.id,
        )
    finally:
        conn.close()

    assert missing == []
    assert resolved_map == {
        "kanban-worker": str(kanban_worker_dir),
        "shared-only": str(shared_skill_dir),
    }
    assert str(kanban_worker_dir) in resolved_args
    assert str(shared_skill_dir) in resolved_args
    assert "shared-only" in loaded
    # The resolver restores module globals after the scoped probe.
    assert skills_tool.SKILLS_DIR == kanban_home / "skills"


def test_forced_skill_disabled_in_worker_profile_stays_denied(kanban_home):
    """Default-profile fallback must not bypass a worker profile's denylist."""
    from hermes_cli.profiles import get_profile_dir
    worker_home = get_profile_dir("worker")
    worker_home.mkdir(parents=True, exist_ok=True)
    (worker_home / "config.yaml").write_text(
        "skills:\n  disabled:\n    - shared-only\n",
        encoding="utf-8",
    )
    _write_test_skill(kanban_home, "kanban-worker")
    _write_test_skill(kanban_home, "shared-only")

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="disabled forced skill",
            assignee="worker",
            skills=["shared-only"],
        )
        task = kb.get_task(conn, tid)
        preflight = kb._preflight_task_skills(task)
    finally:
        conn.close()

    assert preflight is not None
    assert preflight["missing_skills"] == ["shared-only"]
    assert "Unknown skill(s): shared-only" in preflight["error"]


def test_detect_crashed_workers_provider_refusal_is_not_protocol_violation(kanban_home):
    """A clean worker exit with a provider cyber-refusal in the log should
    be classified as a structured provider refusal, not generic protocol
    violation / vague backend issue.
    """
    import hermes_cli.kanban_db as _kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="authorized service exposure audit", assignee="worker")
        host_prefix = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
        fake_pid = 999996
        kb._set_worker_pid(conn, tid, fake_pid)

        log_dir = kb.worker_logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{tid}.log").write_text(
            "Provider: openai-codex\n"
            "Model: gpt-5.5\n"
            "This content was flagged for possible cybersecurity risk.\n",
            encoding="utf-8",
        )

        _kb._record_worker_exit(fake_pid, 0)
        original_alive = _kb._pid_alive
        _kb._pid_alive = lambda p: False
        try:
            result_crashed = kb.detect_crashed_workers(conn)
        finally:
            _kb._pid_alive = original_alive

        assert tid in result_crashed
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.last_failure_error
        assert "blocked_provider_policy" in task.last_failure_error
        assert "protocol violation" not in task.last_failure_error.lower()

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert "provider_refusal" in kinds
        assert "protocol_violation" not in kinds
        assert "gave_up" in kinds

        refusal = [e for e in events if e.kind == "provider_refusal"][-1]
        assert refusal.payload["failure_class"] == "model_api_error"
        assert refusal.payload["failure_subtype"] == "blocked_provider_policy"
        assert refusal.payload["provider_failure_kind"] == "provider_refusal"
        assert refusal.payload["provider"] == "openai-codex"
        assert refusal.payload["model"] == "gpt-5.5"

        gave_up = [e for e in events if e.kind == "gave_up"][-1]
        assert gave_up.payload["trigger_outcome"] == "provider_refusal"
        assert gave_up.payload["provider_failure_kind"] == "provider_refusal"
        assert "model provider" in gave_up.payload["public_message"]
        assert "security-adjacent" in gave_up.payload["public_message"]

        run = kb.latest_run(conn, tid)
        assert run.outcome == "provider_refusal"
        assert run.metadata["failure_subtype"] == "blocked_provider_policy"
    finally:
        conn.close()
