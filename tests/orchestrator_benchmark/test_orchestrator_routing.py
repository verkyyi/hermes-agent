"""Suite B harness validation — proves the routing RUBRIC is correct, for free.

Two layers:
  * Mock layer (always runs): builds temp boards that simulate good and
    deliberately-broken orchestrator output, asserting score_routing() grades
    each correctly. This validates the rubric without any LLM/API cost.
  * Real layer (opt-in, HERMES_RUN_LLM_EVALS=1): runs the actual orchestrator
    over the dataset and asserts an aggregate pass-rate. Skipped by default.

The mock layer is what guards CI; the real layer is the actual eval you fire
on demand once the orchestrator is live.
"""

from __future__ import annotations

import os
import pytest

from hermes_cli import kanban_db as kb
from evals.orchestrator_routing.score import score_routing
from evals.orchestrator_routing.dataset import CASES

# kanban_home fixture is provided by conftest.py in this directory.


def _orch_create(conn, title, assignee, parents, mode):
    """Simulate one kanban_create tool call by the orchestrator."""
    tid = kb.create_task(conn, title=title, body="x" * 600, assignee=assignee, parents=list(parents))
    kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="6625666157",
                      notification_mode=mode)
    return tid


# ---------------------------------------------------------------------------
# Mock layer — rubric validation (no LLM)
# ---------------------------------------------------------------------------

def test_rubric_passes_good_single(kanban_home):
    conn = kb.connect()
    try:
        case = {"kind": "single", "expected_assignees": {"worker-research"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        _orch_create(conn, "do research", "worker-research", [root], "synthesize")
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert r["passed"], r["components"]
    finally:
        conn.close()


def test_rubric_fails_when_subtask_not_linked(kanban_home):
    """The keystone failure: orchestrator routed but did NOT link to root."""
    conn = kb.connect()
    try:
        case = {"kind": "single", "expected_assignees": {"worker-research"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        _orch_create(conn, "do research", "worker-research", [], "synthesize")  # no parents!
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert not r["passed"]
        assert r["components"]["linked"] is False
    finally:
        conn.close()


def test_rubric_fails_wrong_worker(kanban_home):
    conn = kb.connect()
    try:
        case = {"kind": "single", "expected_assignees": {"worker-research"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        _orch_create(conn, "do research", "worker-ops", [root], "synthesize")  # wrong lane
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert not r["passed"]
        assert r["components"]["correct_assignee"] is False
    finally:
        conn.close()


def test_rubric_fails_when_orchestrator_does_not_self_complete(kanban_home):
    conn = kb.connect()
    try:
        case = {"kind": "single", "expected_assignees": {"worker-research"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        _orch_create(conn, "do research", "worker-research", [root], "synthesize")
        # root left open
        r = score_routing(conn, root, case)
        assert not r["passed"]
        assert r["components"]["self_completed"] is False
    finally:
        conn.close()


def test_rubric_passes_good_multi_fanout_fanin(kanban_home):
    conn = kb.connect()
    try:
        case = {"kind": "multi", "expected_assignees": {"worker-research", "worker-ops"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        a = _orch_create(conn, "research", "worker-research", [root], "silent")
        b = _orch_create(conn, "write file", "worker-ops", [root], "silent")
        _orch_create(conn, "consolidate", "worker-fast", [a, b], "synthesize")  # fan-in
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert r["passed"], r["components"]
    finally:
        conn.close()


def test_rubric_passes_multi_pipeline_dag(kanban_home):
    """[strict fan-in] A pipeline DAG (research -> ops -> synth) converges to one sink."""
    conn = kb.connect()
    try:
        case = {"kind": "multi", "expected_assignees": {"worker-research", "worker-ops"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        a = _orch_create(conn, "research", "worker-research", [root], "silent")
        b = _orch_create(conn, "write file", "worker-ops", [a], "silent")  # ops waits on research
        _orch_create(conn, "consolidate", "worker-fast", [a, b], "synthesize")  # sink
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert r["passed"], r["components"]
        assert r["components"]["single_sink"] and r["components"]["all_converge"]
    finally:
        conn.close()


def test_rubric_fails_multi_without_fanin(kanban_home):
    """[strict fan-in] Two independent leaves, no single sink -> FAIL (scattered notifications)."""
    conn = kb.connect()
    try:
        case = {"kind": "multi", "expected_assignees": {"worker-research", "worker-ops"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        _orch_create(conn, "research", "worker-research", [root], "synthesize")  # two sinks
        _orch_create(conn, "write file", "worker-ops", [root], "synthesize")
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert not r["passed"]
        assert r["components"]["single_sink"] is False
        assert r["components"]["fanin_ok"] is False
    finally:
        conn.close()


def test_rubric_fails_when_fanin_not_synthesize(kanban_home):
    """[strict fan-in mode] Correct structure but the sink isn't synthesize -> FAIL."""
    conn = kb.connect()
    try:
        case = {"kind": "multi", "expected_assignees": {"worker-research", "worker-ops"}}
        root = kb.create_task(conn, title="req", body="x" * 600, assignee="orchestrator")
        a = _orch_create(conn, "research", "worker-research", [root], "silent")
        b = _orch_create(conn, "write file", "worker-ops", [root], "silent")
        _orch_create(conn, "consolidate", "worker-fast", [a, b], "direct")  # sink not synthesize
        kb.complete_task(conn, root, result="routed")
        r = score_routing(conn, root, case)
        assert r["components"]["single_sink"] and r["components"]["all_converge"]
        assert r["components"]["fanin_synthesize"] is False
        assert not r["passed"]
    finally:
        conn.close()


def test_rubric_clarify_requires_block(kanban_home):
    """[clarify] Unroutable task: passes iff the orchestrator BLOCKED and created no work."""
    conn = kb.connect()
    try:
        case = {"kind": "clarify", "expected_assignees": set()}
        # correct: blocked, no children
        blocked = kb.create_task(conn, title="vague", body="x" * 50, assignee="orchestrator")
        kb.block_task(conn, blocked, reason="no actionable detail")
        assert score_routing(conn, blocked, case)["passed"]
        # wrong: routed something instead of blocking
        routed = kb.create_task(conn, title="vague2", body="x" * 50, assignee="orchestrator")
        _orch_create(conn, "guess", "worker", [routed], "synthesize")
        kb.complete_task(conn, routed, result="guessed")
        assert not score_routing(conn, routed, case)["passed"]
    finally:
        conn.close()


def test_dataset_is_well_formed():
    assert len(CASES) >= 6
    for c in CASES:
        assert c["kind"] in {"single", "multi", "clarify"}
        assert c["body"].strip()
        if c["kind"] != "clarify":
            assert c["expected_assignees"]


# ---------------------------------------------------------------------------
# Real layer — actual orchestrator LLM eval (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("HERMES_RUN_LLM_EVALS") != "1",
    reason="real orchestrator eval: set HERMES_RUN_LLM_EVALS=1 (costs API calls)",
)
def test_real_orchestrator_routing_pass_rate():
    from evals.orchestrator_routing.run import run_eval
    report = run_eval(trials=int(os.environ.get("HERMES_EVAL_TRIALS", "3")))
    assert report["pass_rate"] >= 0.8, report
