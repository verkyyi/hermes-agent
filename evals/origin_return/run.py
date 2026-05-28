#!/usr/bin/env python
"""Standalone e2e (REAL LLM) for Kanban origin-return — run OUTSIDE pytest.

Why a script, not a pytest: the suite forces HERMES_HOME to a tmp dir and
``hermes_cli/auth.py`` refuses the real auth store while ``PYTEST_CURRENT_TEST``
is set, so an in-process real-LLM turn can't load the real model/creds under
pytest. As a normal process with the real profile, everything resolves. The
board is isolated via ``HERMES_KANBAN_DB`` so this never touches the live board.

Covers the two real-flow gaps:
  (a) A front-desk Telegram turn that creates a Kanban task attaches an origin
      notification subscription (so the completion can return to the user).
  (b) The orchestrator working its task SELF-PARKS via kanban_decompose (the
      task stays alive as the fan-in anchor) instead of completing immediately
      with a routing non-answer.

Usage:
    ./venv/bin/python evals/origin_return/run.py [--phase a|b|all]

Exit 0 = all selected phases passed; non-zero = a failure (TDD red/green).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile


CHAT_ID = "e2e-telegram-chat"


def _isolate_board() -> str:
    """Point HERMES_KANBAN_DB at a fresh tmp board; init it. Returns the path."""
    d = tempfile.mkdtemp(prefix="origin-return-e2e-")
    db = os.path.join(d, "kanban.db")
    os.environ["HERMES_KANBAN_DB"] = db
    os.environ.pop("HERMES_KANBAN_TASK", None)
    os.environ.pop("HERMES_KANBAN_BOARD", None)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    return db


def _build_frontdesk_agent(chat_id: str):
    """A real front-desk agent with a Telegram origin injected (the gateway path
    that `hermes chat` — cli source — can't reproduce)."""
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.oneshot import _create_session_db_for_oneshot
    from run_agent import AIAgent

    cfg = load_config()
    mc = cfg.get("model") or {}
    model = mc if isinstance(mc, str) else (mc.get("default") or mc.get("model") or "")
    rt = resolve_runtime_provider(requested=None, target_model=model or None)

    agent = AIAgent(
        api_key=rt.get("api_key"),
        base_url=rt.get("base_url"),
        provider=rt.get("provider"),
        api_mode=rt.get("api_mode"),
        model=model,
        enabled_toolsets=["kanban"],
        quiet_mode=True,
        platform="telegram",
        chat_id=chat_id,
        user_id="e2e-user",
        session_db=_create_session_db_for_oneshot(),
        credential_pool=rt.get("credential_pool"),
        clarify_callback=lambda *a, **k: "[no user available; pick a sensible default and continue]",
    )
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None
    return agent


def phase_a() -> tuple[bool, str, str | None]:
    """(a) Front-desk create attaches an origin subscription."""
    from hermes_cli import kanban_db as kb

    agent = _build_frontdesk_agent(CHAT_ID)
    agent.chat(
        "Please delegate this to the Kanban board for a worker to handle: "
        "research a couple of good tech/startup events in the Bay Area this weekend. "
        "Create the task and let me know it's queued."
    )

    conn = kb.connect()
    try:
        tasks = conn.execute("SELECT id, assignee, status FROM tasks ORDER BY created_at").fetchall()
        results = [(dict(t), [dict(s) for s in kb.list_notify_subs(conn, t["id"])]) for t in tasks]
    finally:
        conn.close()

    if not results:
        return False, "front-desk turn created NO Kanban task", None
    subbed = [
        (t, s) for (t, s) in results
        if any(x.get("platform") == "telegram" and x.get("chat_id") == CHAT_ID for x in s)
    ]
    summary = [(t["id"], t["assignee"], len(s)) for t, s in results]
    if not subbed:
        return False, f"task created but NO origin sub attached: {summary}", results[0][0]["id"]
    return True, f"origin sub attached: {subbed[0][0]['id']} (board: {summary})", subbed[0][0]["id"]


def phase_b(task_id: str) -> tuple[bool, str]:
    """(b) After the orchestrator works a subscribed anchor task, the origin
    return path must SURVIVE — the origin subscription must end up on a NON-DONE
    task, whether the orchestrator self-parks (sub stays on the parked anchor) or
    create+completes (sub propagates to the pending child that holds the work).
    A sub left only on a done task means the user's real answer can never return.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles as profiles_mod

    if not profiles_mod.profile_exists("orchestrator"):
        return False, "orchestrator profile not installed; cannot run phase (b)"

    orch_home = profiles_mod.resolve_profile_env("orchestrator")
    env = dict(os.environ)
    env["HERMES_HOME"] = orch_home
    env["HERMES_KANBAN_TASK"] = task_id
    cmd = [
        "hermes", "-p", "orchestrator",
        "--skills", "kanban-worker", "--skills", "kanban-orchestrator",
        "chat", "-q", f"work kanban task {task_id}", "-Q",
    ]
    timeout = int(os.environ.get("HERMES_EVAL_ORCH_TIMEOUT", "300"))
    try:
        subprocess.run(cmd, env=env, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, f"orchestrator run timed out after {timeout}s"

    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT s.task_id AS task_id, t.status AS status "
            "FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
            "WHERE s.platform = 'telegram' AND s.chat_id = ?",
            (CHAT_ID,),
        ).fetchall()
        anchor = kb.get_task(conn, task_id)
    finally:
        conn.close()

    placements = [(r["task_id"], r["status"]) for r in rows]
    live = [p for p in placements if p[1] not in ("done", "archived")]
    if not placements:
        return False, "origin subscription disappeared entirely — answer can't return"
    if not live:
        return False, (
            f"origin sub only on done/archived task(s) — real answer can't return: "
            f"{placements} (anchor status={getattr(anchor, 'status', '?')})"
        )
    return True, (
        f"return path survives: origin sub on live task {live[0][0]} "
        f"(status={live[0][1]}); anchor status={getattr(anchor, 'status', '?')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["a", "b", "all"], default="all")
    args = ap.parse_args()

    if os.environ.get("PYTEST_CURRENT_TEST"):
        print("ERROR: run this OUTSIDE pytest (real creds are blocked under pytest).")
        return 2

    _isolate_board()
    failures = []

    tid = None
    if args.phase in ("a", "all"):
        ok, msg, tid = phase_a()
        print(f"[phase a] {'PASS' if ok else 'FAIL'}: {msg}")
        if not ok:
            failures.append("a")

    if args.phase in ("b", "all"):
        if tid is None:
            from hermes_cli import kanban_db as kb
            conn = kb.connect()
            try:
                tid = kb.create_task(conn, title="research weekend Bay Area events", assignee="orchestrator")
                # Give the anchor an origin subscription (as the front-desk would
                # in phase a) so phase b can assess whether the return path survives.
                kb.add_notify_sub(conn, task_id=tid, platform="telegram",
                                  chat_id=CHAT_ID, notification_mode="synthesize")
                kb.recompute_ready(conn)
            finally:
                conn.close()
        ok, msg = phase_b(tid)
        print(f"[phase b] {'PASS' if ok else 'FAIL'}: {msg}")
        if not ok:
            failures.append("b")

    print(f"\nRESULT: {'ALL PASS' if not failures else 'FAILED: ' + ','.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
