"""Responsiveness suite — wraps evals.responsiveness (automated, deterministic).

Score is a composite of the three policy metrics that gate the underlying
benchmark: ack accuracy, the complement of the false-ack rate, and progress
cadence. ``passed`` mirrors the underlying benchmark's own pass/fail.
"""

from __future__ import annotations


def run() -> dict:
    from evals.responsiveness.run import run_benchmark

    report = run_benchmark()
    m = report["metrics"]

    ack_acc = float(m["ack_accuracy"])
    false_ack = min(1.0, float(m["false_ack_rate"]))
    cadence = float(m["cadence_ok_rate"])
    score = 100.0 * (0.4 * ack_acc + 0.3 * (1.0 - false_ack) + 0.3 * cadence)

    return {
        "score": round(score, 2),
        "passed": bool(report["passed"]),
        "metrics": {
            "ack_accuracy": round(ack_acc, 4),
            "false_ack_rate": round(false_ack, 4),
            "cadence_ok_rate": round(cadence, 4),
            "long_work_p95_ttff_s": round(float(m["long_work_p95_ttff_s"]), 3),
            "turns_total": int(m["turns_total"]),
        },
    }
