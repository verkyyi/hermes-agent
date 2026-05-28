"""Kanban-scale suite — wraps tests/stress/test_benchmarks.py (automated, core).

That benchmark deliberately mutates os.environ (HERMES_HOME/HOME) and rmtree's
temp dirs, so it MUST run in an isolated subprocess — running it in-process
would clobber the runner's HERMES_HOME and the trend store path. We collect its
JSON output and score it against coarse absolute ceilings. Those ceilings only
catch gross (order-of-magnitude) regressions; finer drift is surfaced by the
report's delta-vs-baseline using the per-bench medians stored here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BENCH = _REPO_ROOT / "tests" / "stress" / "test_benchmarks.py"

# Any single bench whose median exceeds this is treated as a gross regression.
_CEILING_MS = float(os.environ.get("HERMES_BENCH_KANBAN_CEILING_MS", "5000"))
_TIMEOUT_S = int(os.environ.get("HERMES_BENCH_KANBAN_TIMEOUT_S", "900"))


def run() -> dict:
    if not _BENCH.exists():
        return {"skipped": True, "skip_reason": f"missing {_BENCH}"}

    with tempfile.TemporaryDirectory(prefix="hb-kanban-") as td:
        out = Path(td) / "results.json"
        env = dict(os.environ)
        env["KANBAN_BENCH_OUT"] = str(out)
        proc = subprocess.run(
            [sys.executable, str(_BENCH)],
            cwd=str(_REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
        if not out.exists():
            tail = (proc.stderr or proc.stdout or "")[-500:]
            return {
                "skipped": True,
                "skip_reason": f"bench produced no output (rc={proc.returncode}): {tail}",
            }
        results = json.loads(out.read_text())

    medians = {r["label"]: round(float(r["median_ms"]), 2) for r in results}
    worst = max(medians.values()) if medians else 0.0
    under = sum(1 for v in medians.values() if v <= _CEILING_MS)
    total = len(medians) or 1
    score = 100.0 * under / total

    return {
        "score": round(score, 2),
        "passed": worst <= _CEILING_MS,
        "metrics": {
            "worst_median_ms": worst,
            "ceiling_ms": _CEILING_MS,
            "bench_count": len(medians),
            "medians_ms": medians,
        },
    }
