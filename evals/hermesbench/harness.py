"""HermesBench v2 — isolated default-profile driver (black box).

Sends one prompt to the default profile exactly as an end user would
(`hermes chat -q <prompt> --quiet`) inside a throwaway HERMES_HOME, and reports
only what's observable from outside: the reply text and mechanical reliability
signals (did it respond, how fast, did it stay stable, did it reach a terminal
conclusion within budget). No kanban/orchestrator internals are inspected.

Each call runs in its own temp HERMES_HOME (config.yaml + .env + context-length
cache copied from the real default profile), so runs never pollute real chats or
the production board, and a per-home telemetry.db gives unambiguous latency.
Built on the same isolation pattern as evals/responsiveness/run_live.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

_ERROR_STATUSES = {"error", "failed", "failure", "exception"}


def _hermes_argv() -> list[str]:
    try:
        from hermes_cli import kanban_db as kb
        return list(kb._resolve_hermes_argv())  # type: ignore[attr-defined]
    except Exception:
        return ["hermes"]


def _default_home() -> Path:
    from hermes_cli.profiles import resolve_profile_env
    return Path(resolve_profile_env("default"))


def _make_isolated_home(src_home: Path) -> Path:
    home = Path(tempfile.mkdtemp(prefix="hb-usecase-"))
    for name in ("config.yaml", ".env", "context_length_cache.yaml"):
        s = src_home / name
        if s.exists():
            shutil.copy2(s, home / name)
            try:
                (home / name).chmod(0o600)
            except OSError:
                pass
    return home


def _read_turn_row(home: Path) -> dict | None:
    db = home / "telemetry.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ttfa_ms, ttft_ms, ttlt_ms, status, model, output_chars "
            "FROM turns ORDER BY started_at_ms DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def run_case(prompt: str, *, timeout_s: int, src_home: Path | None = None) -> dict:
    """Drive one isolated default-profile turn. Returns reply + mechanical signals.

    Mechanical signals (no LLM judgement):
      responded  — process exited 0 with a non-empty reply
      stable     — exited 0, not a timeout, telemetry status not an error
      concluded  — responded AND not timed out (a terminal reply arrived in budget)
      ttfa_ms / ttlt_ms / wall_ms — latency (telemetry; wall is the fallback)
    """
    src_home = src_home or _default_home()
    home = _make_isolated_home(src_home)
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_PROFILE"] = "default"
    env.pop("HERMES_KANBAN_TASK", None)
    cmd = [*_hermes_argv(), "chat", "-q", prompt, "--quiet"]

    reply, rc, timed_out, err = "", None, False, None
    wall0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_s)
        rc = proc.returncode
        reply = (proc.stdout or "").strip()
        if rc != 0 and not reply:
            err = (proc.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        rc, timed_out, err = 124, True, f"timeout after {timeout_s}s"
    finally:
        wall_ms = round((time.monotonic() - wall0) * 1000.0, 1)
        row = _read_turn_row(home)
        shutil.rmtree(home, ignore_errors=True)

    status = (row or {}).get("status")
    responded = (rc == 0) and bool(reply)
    stable = (rc == 0) and (not timed_out) and (str(status or "").lower() not in _ERROR_STATUSES)
    concluded = responded and not timed_out

    return {
        "prompt": prompt,
        "reply": reply,
        "returncode": rc,
        "timed_out": timed_out,
        "responded": responded,
        "stable": stable,
        "concluded": concluded,
        "ttfa_ms": (row or {}).get("ttfa_ms"),
        "ttft_ms": (row or {}).get("ttft_ms"),
        "ttlt_ms": (row or {}).get("ttlt_ms"),
        "wall_ms": wall_ms,
        "telemetry_status": status,
        "model": (row or {}).get("model"),
        "error": err,
    }
