"""HermesBench v2 use-case suites — one per category.

For each case in a category we drive the default profile in an isolated turn
(harness), then judge the reply (judge). The category score weights reliability
far above capability:

    score = 100 · (0.40·closure + 0.20·stable + 0.15·responsiveness + 0.25·appropriate)

Closure is a hard gate: `passed` requires every trial to reach a genuine
terminal conclusion and stay stable. A correct-but-never-concluding turn fails.

All suites self-skip without HERMES_RUN_LLM_EVALS (they drive real agents).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from evals.hermesbench import harness, usecases
from evals.hermesbench import judge as judge_mod

TRIALS = int(os.environ.get("HERMES_BENCH_TRIALS", "2"))
CONCURRENCY = int(os.environ.get("HERMES_BENCH_CONCURRENCY", "4"))
APPROPRIATE_PASS = float(os.environ.get("HERMES_BENCH_APPROPRIATE_PASS", "0.7"))


def _responsiveness(ttfa_ms, wall_ms, reply_target_s: float) -> float:
    """0..1 time-to-reply score: full credit at/under target, linear decay to 0 at 3×.

    Prefers telemetry ttfa_ms (true first-answer latency) when present; in the
    one-shot `chat -q` harness that's usually absent, so it falls back to
    wall-clock (total time to the single reply).
    """
    t_ms = ttfa_ms if ttfa_ms is not None else wall_ms
    if t_ms is None:
        return 0.0
    t = t_ms / 1000.0
    budget = max(0.1, reply_target_s)
    if t <= budget:
        return 1.0
    return max(0.0, 1.0 - (t - budget) / (2.0 * budget))


def _p50(xs: list[float]):
    vals = sorted(v for v in xs if v is not None)
    return vals[len(vals) // 2] if vals else None


def _run_trial(case: dict, b: dict) -> dict:
    m = harness.run_case(case["prompt"], timeout_s=int(b["conclude_s"]) + 30)
    v = judge_mod.judge(case, m["reply"])
    genuine = bool(m["concluded"]) and v["conclusion_type"] != "none"
    return {
        "case": case["id"],
        "expectation": case.get("expectation"),
        "mech": m,
        "judge": v,
        "genuine_conclusion": genuine,
        "responsiveness": _responsiveness(m.get("ttfa_ms"), m.get("wall_ms"), b["reply_target_s"]),
    }


def _run_category(category: str) -> dict:
    if not os.environ.get("HERMES_RUN_LLM_EVALS"):
        return {"skipped": True, "skip_reason": "HERMES_RUN_LLM_EVALS not set"}

    cases = usecases.cases_for(category)
    b = usecases.budget(category)
    if not cases:
        return {"skipped": True, "skip_reason": f"no cases for {category}"}

    jobs = [c for c in cases for _ in range(TRIALS)]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY)) as pool:
        futs = [pool.submit(_run_trial, c, b) for c in jobs]
        for f in as_completed(futs):
            results.append(f.result())

    n = len(results) or 1
    closure_rate = sum(1 for r in results if r["genuine_conclusion"]) / n
    stable_rate = sum(1 for r in results if r["mech"]["stable"]) / n
    responded_rate = sum(1 for r in results if r["mech"]["responded"]) / n
    resp_mean = sum(r["responsiveness"] for r in results) / n

    judged = [r for r in results if not r["judge"]["judge_error"]]
    appropriate_mean = (sum(r["judge"]["appropriate"] for r in judged) / len(judged)) if judged else 0.0
    coherent_mean = (sum(r["judge"]["coherent"] for r in judged) / len(judged)) if judged else 0.0

    score = 100.0 * (0.40 * closure_rate + 0.20 * stable_rate
                     + 0.15 * resp_mean + 0.25 * appropriate_mean)
    passed = (closure_rate >= 1.0 and stable_rate >= 1.0
              and appropriate_mean >= APPROPRIATE_PASS)

    ctypes: dict = {}
    for r in results:
        ct = r["judge"]["conclusion_type"]
        ctypes[ct] = ctypes.get(ct, 0) + 1

    # A short sample of what went wrong, for the run JSON.
    failures = [
        {"case": r["case"], "conclusion": r["judge"]["conclusion_type"],
         "stable": r["mech"]["stable"], "reason": r["judge"]["reason"][:160]}
        for r in results
        if not r["genuine_conclusion"] or not r["mech"]["stable"]
    ][:5]

    return {
        "score": round(score, 2),
        "passed": passed,
        "metrics": {
            "trials": n,
            "cases": len(cases),
            "closure_rate": round(closure_rate, 3),
            "stable_rate": round(stable_rate, 3),
            "responded_rate": round(responded_rate, 3),
            "responsiveness_mean": round(resp_mean, 3),
            "appropriate_mean": round(appropriate_mean, 3),
            "coherent_mean": round(coherent_mean, 3),
            "ttfa_p50_ms": _p50([r["mech"].get("ttfa_ms") for r in results]),
            "ttlt_p50_ms": _p50([r["mech"].get("ttlt_ms") for r in results]),
            "wall_p50_ms": _p50([r["mech"].get("wall_ms") for r in results]),
            "conclusion_types": ctypes,
            "judge_errors": sum(1 for r in results if r["judge"]["judge_error"]),
            "failures": failures,
        },
    }


# One run() per category — registry maps these (no-arg) callables to suites.
def run_direct_answer() -> dict: return _run_category("direct_answer")
def run_quick_task() -> dict: return _run_category("quick_task")
def run_multistep() -> dict: return _run_category("multistep")
def run_ambiguous() -> dict: return _run_category("ambiguous")
def run_refusal() -> dict: return _run_category("refusal")
