"""Orchestrator suite — wraps evals.orchestrator_routing (hybrid; needs a model).

Spawns the real orchestrator over isolated throwaway boards and scores routing /
linking / notification correctness. Live: needs HERMES_RUN_LLM_EVALS and the
orchestrator profile installed. Trials default to 1 to bound daily token cost
(override with HERMES_EVAL_TRIALS).
"""

from __future__ import annotations

import os

_PASS_THRESHOLD = float(os.environ.get("HERMES_BENCH_ORCH_PASS", "0.8"))


def run() -> dict:
    if not os.environ.get("HERMES_RUN_LLM_EVALS"):
        return {"skipped": True, "skip_reason": "HERMES_RUN_LLM_EVALS not set"}

    from evals.orchestrator_routing.run import DEFAULT_CONCURRENCY, run_eval

    trials = int(os.environ.get("HERMES_EVAL_TRIALS", "1"))
    rep = run_eval(trials=trials, concurrency=DEFAULT_CONCURRENCY)

    pass_rate = float(rep.get("pass_rate", 0.0))
    per_case = rep.get("per_case", {})
    return {
        "score": round(100.0 * pass_rate, 2),
        "passed": pass_rate >= _PASS_THRESHOLD,
        "metrics": {
            "pass_rate": round(pass_rate, 4),
            "trials_per_case": trials,
            "cases": len(per_case),
            "per_case_rate": {k: round(v.get("rate", 0.0), 3) for k, v in per_case.items()},
        },
    }
