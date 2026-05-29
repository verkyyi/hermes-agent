"""HermesBench — single consolidated benchmark runner.

    venv/bin/python -m evals.hermesbench.run                 # run all suites
    venv/bin/python -m evals.hermesbench.run --suite responsiveness,kanban_scale
    venv/bin/python -m evals.hermesbench.run --json          # machine-readable
    venv/bin/python -m evals.hermesbench.run --no-store      # don't persist

Runs every registered suite. The model-backed suites (orchestrator,
origin_return) self-skip when HERMES_RUN_LLM_EVALS is unset, so a creds-less run
degrades cleanly to the deterministic suites and is recorded as skipped (not
failed). Exits non-zero if any suite that actually ran failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from evals.hermesbench import registry, report as report_mod, store


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _model_id() -> str | None:
    # Best-effort, pinned for the trend; never fail the run over it.
    env = os.environ.get("HERMES_BENCH_MODEL_ID")
    if env:
        return env
    try:
        import yaml  # type: ignore

        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        cfg = Path(home) / "config.yaml"
        if cfg.exists():
            data = yaml.safe_load(cfg.read_text()) or {}
            model = data.get("model") or (data.get("models") or {}).get("main")
            if isinstance(model, dict):  # e.g. {default: ..., provider: ...}
                return model.get("default") or json.dumps(model, sort_keys=True)
            return str(model) if model is not None else None
    except Exception:
        pass
    return None


def _profile_hash() -> str | None:
    try:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        cfg = Path(home) / "config.yaml"
        if cfg.exists():
            return hashlib.sha256(cfg.read_bytes()).hexdigest()
    except Exception:
        pass
    return None


def _execute(suite: registry.Suite) -> dict:
    base = {
        "id": suite.id, "category": suite.category, "mode": suite.mode,
        "weight": suite.weight, "summary": suite.summary,
        "score": None, "passed": None, "skipped": False,
        "skip_reason": None, "error": None, "duration_s": 0.0, "metrics": {},
    }
    # Suites that need a model self-skip when HERMES_RUN_LLM_EVALS is unset
    # (see their run()), so there's no gating to do here.
    t0 = time.perf_counter()
    try:
        out = suite.load()()
    except Exception as exc:  # a broken suite must not sink the whole run
        base["error"] = f"{type(exc).__name__}: {exc}"[:400]
        base["duration_s"] = round(time.perf_counter() - t0, 3)
        return base
    base["duration_s"] = round(time.perf_counter() - t0, 3)

    if out.get("skipped"):
        base.update(skipped=True, skip_reason=out.get("skip_reason"))
        return base
    base["score"] = out.get("score")
    base["passed"] = out.get("passed")
    base["metrics"] = out.get("metrics") or {}
    return base


def run_benchmark(*, ids: list[str] | None = None) -> dict:
    suites = registry.select(ids=ids)

    results = [_execute(s) for s in suites]

    ran = [r for r in results if not r["skipped"] and not r["error"] and r["score"] is not None]
    if ran:
        w = sum(r["weight"] for r in ran) or 1.0
        overall = round(sum(r["weight"] * r["score"] for r in ran) / w, 2)
    else:
        overall = None
    # A run passes if nothing that actually ran failed (errors count as failures).
    failed = [r for r in results if r["error"] or r["passed"] is False]
    passed = len(failed) == 0

    now = datetime.now(timezone.utc)
    return {
        "run_id": "hb-" + now.strftime("%Y%m%dT%H%M%SZ"),
        "ts": now.isoformat(),
        "overall_score": overall,
        "passed": passed,
        "suites_ran": len(ran),
        "harness": {
            "git_sha": _git_sha(),
            "model_id": _model_id(),
            "profile_hash": _profile_hash(),
        },
        "suites": results,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermesbench")
    ap.add_argument("--suite", help="comma-separated suite ids to restrict to")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-store", action="store_true", help="do not persist to the trend store")
    args = ap.parse_args(argv)

    ids = [s for s in (args.suite or "").split(",") if s] or None
    report = run_benchmark(ids=ids)

    previous = None
    if not args.no_store:
        try:
            store.save_run(report)
            previous = store.previous_run(report["run_id"])
        except Exception as exc:
            print(f"warning: could not persist run: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(report_mod.render(report, previous))

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
