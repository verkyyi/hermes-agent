"""Real orchestrator routing eval — invokes the live LLM. Opt-in.

Safety / isolation model (important):
  * Each trial runs on a THROWAWAY board under a temp dir, pinned via
    HERMES_KANBAN_DB. The live gateway dispatcher enumerates only its own
    boards root, so it never sees this board -> the sub-tasks the orchestrator
    creates are NOT spawned. Nothing real executes; they sit for inspection.
  * The orchestrator profile's toolset is [kanban] only, so even a misbehaving
    run can only touch this isolated board.
  * No gateway needed: we spawn the orchestrator the same way the dispatcher
    does (`hermes -p orchestrator --skills kanban-worker chat -q ...`) and wait.

Run via the gated pytest:
    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m pytest tests/evals -q
or directly:
    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.orchestrator_routing.run
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from hermes_cli import kanban_db as kb
from evals.orchestrator_routing.dataset import CASES
from evals.orchestrator_routing.score import score_routing

ORCH_TIMEOUT_S = int(os.environ.get("HERMES_EVAL_ORCH_TIMEOUT", "300"))
# Cap concurrent orchestrator sessions. Parallelism is process-based (each job
# runs in its own process with a process-local os.environ + its own temp board),
# so there is no board-path race. The cap keeps concurrent LLM sessions low
# enough to avoid provider rate-limits — the very route-B failure the design
# guards against. Default 3; tune via HERMES_EVAL_CONCURRENCY.
DEFAULT_CONCURRENCY = int(os.environ.get("HERMES_EVAL_CONCURRENCY", "3"))


def _hermes_argv() -> list:
    try:
        return list(kb._resolve_hermes_argv())  # type: ignore[attr-defined]
    except Exception:
        return ["hermes"]


def _spawn_orchestrator(task_id: str, board_db: Path, env_base: dict, skill_ids: list):
    """Launch the orchestrator on one task, mirroring _default_spawn. Blocks.

    `skill_ids` is the dispatcher's resolved forced-skill list — for an
    orchestrator task that pins kanban-orchestrator this is
    ["kanban-worker", "kanban-orchestrator"] (kanban-worker is always
    force-prepended). Each id becomes its own `--skills` pair, so the
    orchestrator runs WITH its decomposition playbook, not just the worker one.

    Returns (returncode, stdout, stderr) so callers can capture the
    orchestrator's final message for failure diagnostics.
    """
    env = dict(env_base)
    env["HERMES_KANBAN_TASK"] = task_id
    env["HERMES_KANBAN_DB"] = str(board_db)
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(board_db.parent / "workspaces")
    env["HERMES_KANBAN_BOARD"] = "eval"
    try:
        from hermes_cli.profiles import resolve_profile_env
        env["HERMES_HOME"] = resolve_profile_env("orchestrator")
    except Exception:
        pass
    cmd = [*_hermes_argv(), "-p", "orchestrator"]
    for sk in (skill_ids or ["kanban-worker", "kanban-orchestrator"]):
        cmd.extend(["--skills", sk])
    cmd.extend(["chat", "-q", f"work kanban task {task_id}"])
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          timeout=ORCH_TIMEOUT_S)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _dump_tree(conn, root_id: str) -> dict:
    """Full board snapshot for failure diagnosis: every task + how it links."""
    def mode_of(tid):
        subs = kb.list_notify_subs(conn, task_id=tid)
        return subs[0]["notification_mode"] if subs else None
    nodes = []
    for t in kb.list_tasks(conn):
        nodes.append({
            "id": t.id, "status": t.status, "assignee": t.assignee,
            "parents": kb.parent_ids(conn, t.id),
            "mode": mode_of(t.id), "title": (t.title or "")[:50],
        })
    return {"root": root_id, "root_status": kb.get_task(conn, root_id).status,
            "nodes": nodes}


def run_case(case: dict, env_base: dict, trial: int = 0) -> dict:
    """One trial of one case on an isolated board. Returns the score dict.

    Designed to run in its own process (ProcessPoolExecutor): the os.environ
    writes below are process-local, so concurrent jobs never clash on the
    board path. The temp dir is unique per call regardless.
    """
    with tempfile.TemporaryDirectory(prefix="orch-eval-") as d:
        board_db = Path(d) / "kanban.db"
        os.environ["HERMES_KANBAN_DB"] = str(board_db)
        os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = str(Path(d) / "workspaces")
        kb.init_db()
        conn = kb.connect()
        try:
            # Pin the orchestrator playbook on the task, exactly as the default
            # profile SHOULD when it routes work to the orchestrator. The
            # dispatcher force-prepends kanban-worker, so this resolves to
            # ["kanban-worker", "kanban-orchestrator"].
            # A/B toggle: HERMES_EVAL_PIN_ORCH_SKILL=0 omits the skill, matching
            # production TODAY (SOUL + kanban-worker only) — used to decide
            # whether pinning the skill actually changes routing quality.
            pin_skill = os.environ.get("HERMES_EVAL_PIN_ORCH_SKILL", "1") != "0"
            extra_skills = ["kanban-orchestrator"] if pin_skill else None
            root = kb.create_task(conn, title=case["name"], body=case["body"],
                                  assignee="orchestrator", skills=extra_skills)
            skill_ids = kb._forced_skill_identifiers(kb.get_task(conn, root))
        finally:
            conn.close()
        try:
            _rc, stdout, _stderr = _spawn_orchestrator(root, board_db, env_base, skill_ids)
        except subprocess.TimeoutExpired:
            return {"passed": False, "components": {"timeout": True},
                    "case": case["name"], "trial": trial}
        except Exception as exc:  # creds / spawn / unexpected — record, don't abort the pool
            return {"passed": False, "components": {"error": str(exc)[:200]},
                    "case": case["name"], "trial": trial}
        conn = kb.connect()
        try:
            result = score_routing(conn, root, case)
            # On failure, capture WHY: the board tree + the orchestrator's
            # final message (reveals e.g. "I blocked for clarification").
            if not result["passed"]:
                result["diagnostics"] = {
                    "tree": _dump_tree(conn, root),
                    "orchestrator_msg_tail": stdout[-1200:],
                }
        finally:
            conn.close()
        result["case"] = case["name"]
        result["trial"] = trial
        return result


def run_eval(trials: int = 3, concurrency: int = DEFAULT_CONCURRENCY,
             only: list = None) -> dict:
    """Run every case x trials across a capped process pool. Order-independent.

    `only` (or env HERMES_EVAL_ONLY=comma,names) restricts to named cases —
    used to re-run just the failing ones for diagnosis.
    """
    only = only or [s for s in os.environ.get("HERMES_EVAL_ONLY", "").split(",") if s]
    cases = [c for c in CASES if c["name"] in only] if only else CASES
    env_base = dict(os.environ)
    jobs = [(case, t) for case in cases for t in range(trials)]
    results: list[dict] = []
    concurrency = max(1, int(concurrency))

    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(run_case, case, env_base, t): (case["name"], t)
                for (case, t) in jobs}
        done = 0
        for fut in as_completed(futs):
            name, t = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {"passed": False, "components": {"pool_error": str(exc)[:200]},
                     "case": name, "trial": t}
            results.append(r)
            done += 1
            print(f"[{done}/{len(jobs)}] {name} trial {t}: "
                  f"{'PASS' if r.get('passed') else 'FAIL'} {r.get('components')}",
                  file=sys.stderr, flush=True)

    per_case: dict = {}
    for case in cases:
        rs = [r for r in results if r["case"] == case["name"]]
        n_pass = sum(1 for r in rs if r.get("passed"))
        per_case[case["name"]] = {
            "pass": n_pass, "trials": len(rs),
            "rate": n_pass / len(rs) if rs else 0.0,
            "failures": [{k: v for k, v in r.items() if k != "case"}
                         for r in rs if not r.get("passed")],
        }
    total = len(results)
    passes = sum(1 for r in results if r.get("passed"))
    return {"pass_rate": passes / total if total else 0.0,
            "trials_per_case": trials, "concurrency": concurrency,
            "per_case": per_case}


if __name__ == "__main__":
    import json
    rep = run_eval(
        trials=int(os.environ.get("HERMES_EVAL_TRIALS", "3")),
        concurrency=DEFAULT_CONCURRENCY,
    )
    print(json.dumps(rep, indent=2))
