"""Origin-return suite — wraps evals.origin_return (llm_judge, live tier).

Two phases on a real (isolated) board with the real local profile:
  (a) a front-desk turn must create a Kanban task with the origin subscription,
  (b) after orchestrator self-park, the return path must survive on a non-done
      anchor so the user's answer can still come back.

Live: needs HERMES_RUN_LLM_EVALS and real creds. Phase (b) is reported as
skipped (not failed) when the orchestrator profile is not installed.
"""

from __future__ import annotations

import os


def run() -> dict:
    if not os.environ.get("HERMES_RUN_LLM_EVALS"):
        return {"skipped": True, "skip_reason": "HERMES_RUN_LLM_EVALS not set"}

    from evals.origin_return.run import phase_a, phase_b

    ran = 0
    passed = 0
    metrics: dict = {}

    a_ok, a_msg, task_id = phase_a()
    ran += 1
    passed += 1 if a_ok else 0
    metrics["phase_a_pass"] = bool(a_ok)
    metrics["phase_a_msg"] = a_msg[:300]

    if task_id:
        b_ok, b_msg = phase_b(task_id)
        if "profile not installed" in b_msg:
            metrics["phase_b_skipped"] = b_msg[:200]
        else:
            ran += 1
            passed += 1 if b_ok else 0
            metrics["phase_b_pass"] = bool(b_ok)
            metrics["phase_b_msg"] = b_msg[:300]
    else:
        metrics["phase_b_skipped"] = "phase_a produced no task_id"

    score = 100.0 * passed / ran if ran else 0.0
    return {
        "score": round(score, 2),
        "passed": ran > 0 and passed == ran,
        "metrics": metrics,
    }
