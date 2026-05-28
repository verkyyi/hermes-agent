"""Break-glass diagnostics and repair helpers for Hermes/Kanban runtime failures.

This module is intentionally deterministic and local.  It does not call the
model, does not use ``handle_function_call()`` for its own orchestration, and is
safe to invoke when the normal agent tool loop is suspected broken.  Some smoke
checks deliberately exercise ``handle_function_call()`` because that is the
runtime surface they are meant to verify.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.config import get_hermes_home, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TESTS = (
    "tests/test_model_tools.py::TestHandleFunctionCall::test_request_id_is_accepted_and_forwarded_to_registry_and_hooks",
    "tests/test_model_tools.py::TestHandleFunctionCall::test_request_id_reaches_representative_real_tool_handlers",
    "tests/hermes_cli/test_break_glass.py",
)

_FAILURE_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tool_dispatch_metadata_kwarg", ("unexpected keyword argument 'request_id'", "unexpected keyword argument: request_id")),
    ("unknown_forced_skill", ("Unknown skill(s)", "unknown skill")),
    ("tool_protocol_violation", ("tool_call_id", "messages with role 'tool'", "protocol violation")),
    ("stale_worker", ("pid", "not alive", "stale claim", "claim expired")),
    ("provider_refusal", ("refusal", "content policy", "safety policy")),
    ("provider_auth_or_quota", ("401", "403", "rate limit", "quota", "Missing Authentication")),
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def classify_failure(text: str) -> str:
    """Return a stable failure class for common Kanban/runtime breakages."""
    haystack = str(text or "")
    low = haystack.lower()
    for label, needles in _FAILURE_CLASSES:
        for needle in needles:
            if needle.lower() in low:
                return label
    if "handle_function_call" in low or "tool execution failed" in low:
        return "tool_runtime_failure"
    return "unknown"


def _run(cmd: Iterable[str], *, timeout: int = 60, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd or PROJECT_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "duration_ms": int((time.time() - started) * 1000),
            "cmd": list(cmd),
            "output_tail": output[-6000:],
            "failure_class": None if proc.returncode == 0 else classify_failure(output),
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return {
            "ok": False,
            "returncode": None,
            "duration_ms": int((time.time() - started) * 1000),
            "cmd": list(cmd),
            "output_tail": str(output)[-6000:],
            "failure_class": "timeout",
        }


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _python() -> str:
    return sys.executable or "python3"


def _process_snapshot(pid: int | None) -> dict[str, Any]:
    """Return safe process identity details for activation checks."""
    if not pid:
        return {"running": False, "pid": None}
    info: dict[str, Any] = {"running": True, "pid": pid}
    try:
        from gateway.status import get_process_start_time

        info["kernel_start_time"] = get_process_start_time(pid)
    except Exception as exc:
        info["kernel_start_time_error"] = str(exc)
    ps = _run(["ps", "-o", "pid=,ppid=,lstart=,command=", "-p", str(pid)], timeout=10)
    info["ps_ok"] = ps.get("ok")
    info["ps_output"] = ps.get("output_tail", "").strip()
    return info


def _gateway_snapshot() -> dict[str, Any]:
    """Inspect the live gateway without sending messages or using model tools."""
    snapshot: dict[str, Any] = {}
    try:
        from gateway.status import get_running_pid, read_runtime_status

        pid = get_running_pid(cleanup_stale=False)
        snapshot["process"] = _process_snapshot(pid)
        status = read_runtime_status() or {}
        snapshot["runtime_status"] = {
            key: status.get(key)
            for key in (
                "gateway_state",
                "updated_at",
                "restart_requested",
                "active_agents",
                "exit_reason",
                "pid",
                "start_time",
            )
            if key in status
        }
        platforms = status.get("platforms")
        if isinstance(platforms, dict):
            snapshot["platform_count"] = len(platforms)
            snapshot["platform_states"] = {
                str(name): data.get("platform_state") or data.get("state")
                for name, data in platforms.items()
                if isinstance(data, dict)
            }
    except Exception as exc:
        snapshot["error"] = str(exc)
    snapshot["cli_status"] = _run([_python(), "-m", "hermes_cli.main", "gateway", "status"], timeout=30)
    return snapshot


def _kanban_dispatcher_snapshot() -> dict[str, Any]:
    """Summarize Kanban dispatcher activation policy/status from local state."""
    try:
        cfg = load_config()
    except Exception as exc:
        return {"ok": False, "failure_class": "config_error", "error": str(exc)}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    dispatch_in_gateway = bool(kanban_cfg.get("dispatch_in_gateway", True))
    notify_in_gateway = bool(kanban_cfg.get("notify_in_gateway", dispatch_in_gateway))
    gateway = _gateway_snapshot()
    process = gateway.get("process", {}) if isinstance(gateway, dict) else {}
    return {
        "ok": bool((not dispatch_in_gateway) or process.get("running")),
        "dispatch_in_gateway": dispatch_in_gateway,
        "notify_in_gateway": notify_in_gateway,
        "dispatch_interval_seconds": kanban_cfg.get("dispatch_interval_seconds"),
        "status": "embedded_gateway" if dispatch_in_gateway else "external_or_disabled",
        "gateway_pid": process.get("pid"),
    }


def collect_diagnostics() -> dict[str, Any]:
    """Collect local diagnostics without invoking the model or tool dispatch."""
    hermes_home = get_hermes_home()
    files = [
        PROJECT_ROOT / "model_tools.py",
        PROJECT_ROOT / "run_agent.py",
        PROJECT_ROOT / "tools" / "registry.py",
        PROJECT_ROOT / "tools" / "kanban_tools.py",
        PROJECT_ROOT / "hermes_cli" / "break_glass.py",
    ]
    return {
        "ok": True,
        "project_root": str(PROJECT_ROOT),
        "hermes_home": str(hermes_home),
        "python": sys.version.split()[0],
        "python_executable": _python(),
        "platform": platform.platform(),
        "git_head": _run(["git", "rev-parse", "HEAD"], timeout=10)["output_tail"].strip(),
        "git_status": _run(["git", "status", "--short"], timeout=10)["output_tail"].splitlines(),
        "file_hashes": {str(p.relative_to(PROJECT_ROOT)): _sha256(p) for p in files},
        "gateway": _gateway_snapshot(),
        "kanban_dispatcher": _kanban_dispatcher_snapshot(),
    }


@contextlib.contextmanager
def _patched_env(updates: dict[str, str]):
    old: dict[str, str | None] = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_direct_tool_smoke() -> dict[str, Any]:
    """Exercise real Kanban + terminal tool dispatch with metadata kwargs.

    Creates a temporary isolated Kanban DB, claims one task, then calls
    ``kanban_show``, ``terminal``, and ``kanban_complete`` through
    ``handle_function_call(..., request_id=...)``.  The runner itself creates
    and inspects the DB directly, so a failure is classified clearly instead of
    relying on the model loop to explain itself.
    """
    started = _now_ms()
    with tempfile.TemporaryDirectory(prefix="hermes-kanban-smoke-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "kanban.db"
        workspace_root = tmp_path / "workspaces"
        env = {
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(workspace_root),
            "HERMES_KANBAN_BOARD": "break-glass-smoke",
            "HERMES_PROFILE": "break-glass-smoke",
        }
        with _patched_env(env):
            from hermes_cli import kanban_db as kb

            conn = kb.connect(db_path=db_path)
            task_id = kb.create_task(
                conn,
                title="break-glass direct tool-runtime smoke",
                body="Verify kanban_show + terminal + kanban_complete with request_id metadata.",
                assignee="break-glass-smoke",
                created_by="break-glass-smoke",
                max_runtime_seconds=60,
            )
            claimed = kb.claim_task(conn, task_id, ttl_seconds=120, claimer="break-glass-smoke")
            if claimed is None:
                return {"ok": False, "failure_class": "stale_worker", "task_id": task_id, "error": "could not claim smoke task"}
            run_id = getattr(claimed, "current_run_id", None)
            with _patched_env({"HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": str(run_id or "")}):
                from model_tools import handle_function_call

                request_id = f"break-glass-{_now_ms()}"
                steps: list[dict[str, Any]] = []
                calls = [
                    ("kanban_show", {"task_id": task_id}),
                    ("terminal", {"command": "printf hermes-kanban-smoke", "timeout": 30, "workdir": str(tmp_path)}),
                    ("kanban_complete", {"summary": "break-glass smoke passed", "metadata": {"request_id": request_id}}),
                ]
                for name, args in calls:
                    raw = handle_function_call(
                        name,
                        args,
                        task_id=task_id,
                        session_id="break-glass-smoke",
                        request_id=request_id,
                        enabled_tools=["kanban_show", "kanban_complete", "terminal"],
                    )
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        parsed = {"raw": raw}
                    step_ok = not (isinstance(parsed, dict) and parsed.get("error"))
                    steps.append({"tool": name, "ok": step_ok, "result": parsed})
                    if not step_ok:
                        return {
                            "ok": False,
                            "failure_class": classify_failure(raw),
                            "task_id": task_id,
                            "request_id": request_id,
                            "duration_ms": _now_ms() - started,
                            "steps": steps,
                        }
                final = kb.get_task(conn, task_id)
                return {
                    "ok": bool(final and final.status == "done"),
                    "failure_class": None if final and final.status == "done" else "tool_runtime_failure",
                    "task_id": task_id,
                    "request_id": request_id,
                    "duration_ms": _now_ms() - started,
                    "steps": steps,
                    "final_status": final.status if final else None,
                }


def run_worker_smoke(*, profile: str = "worker", timeout: int = 180) -> dict[str, Any]:
    """Run an explicit LLM worker smoke against an isolated DB.

    This is opt-in because it uses provider credentials and takes longer than
    the direct deterministic smoke.  It is useful after a gateway/runtime patch
    to verify the actual worker process can call tools and complete a task.
    """
    with tempfile.TemporaryDirectory(prefix="hermes-kanban-worker-smoke-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "kanban.db"
        workspace_root = tmp_path / "workspaces"
        env = os.environ.copy()
        env.update({
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(workspace_root),
            "HERMES_KANBAN_BOARD": "break-glass-worker-smoke",
            "HERMES_PROFILE": profile,
        })
        from hermes_cli import kanban_db as kb

        with _patched_env({k: env[k] for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD", "HERMES_PROFILE")}):
            conn = kb.connect(db_path=db_path)
            task_id = kb.create_task(
                conn,
                title="break-glass worker smoke",
                body="Use kanban_show, terminal printf hermes-worker-smoke, then kanban_complete.",
                assignee=profile,
                created_by="break-glass",
                max_runtime_seconds=timeout,
                skills=["kanban-worker"],
            )
            claimed = kb.claim_task(conn, task_id, ttl_seconds=timeout + 60, claimer="break-glass-worker-smoke")
            run_id = getattr(claimed, "current_run_id", None) if claimed else None
        env.update({"HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": str(run_id or "")})
        prompt = (
            "Kanban runtime smoke. Do exactly this and no extra work: "
            "1) call kanban_show for your current task; "
            "2) call terminal with command `printf hermes-worker-smoke`; "
            "3) call kanban_complete with summary `worker smoke passed`."
        )
        cmd = [
            _python(), "-m", "hermes_cli.main", "-p", profile,
            "--skills", "kanban-worker", "chat", "-Q", "--toolsets", "kanban,terminal", "-q", prompt,
        ]
        result = _run(cmd, timeout=timeout, env=env)
        with _patched_env({"HERMES_KANBAN_DB": str(db_path), "HERMES_KANBAN_WORKSPACES_ROOT": str(workspace_root), "HERMES_KANBAN_BOARD": "break-glass-worker-smoke"}):
            conn = kb.connect(db_path=db_path)
            final = kb.get_task(conn, task_id)
        result.update({
            "task_id": task_id,
            "final_status": final.status if final else None,
            "ok": bool(result.get("ok") and final and final.status == "done"),
        })
        if not result["ok"] and not result.get("failure_class"):
            result["failure_class"] = classify_failure(result.get("output_tail", ""))
        return result


def run_targeted_tests() -> dict[str, Any]:
    return _run([_python(), "-m", "pytest", *DEFAULT_TESTS, "-q", "-o", "addopts="], timeout=240)


def run_py_compile() -> dict[str, Any]:
    files = [
        "hermes_cli/break_glass.py",
        "hermes_cli/main.py",
        "model_tools.py",
        "tools/kanban_tools.py",
    ]
    return _run([_python(), "-m", "py_compile", *files], timeout=60)


def verify_activation(*, restart_gateway: bool = False, worker_smoke: bool = False) -> dict[str, Any]:
    before_gateway = _gateway_snapshot()
    result: dict[str, Any] = {
        "diagnostics": collect_diagnostics(),
        "gateway_before": before_gateway,
        "py_compile": run_py_compile(),
        "direct_smoke": run_direct_tool_smoke(),
    }
    if restart_gateway:
        result["gateway_restart"] = _run([_python(), "-m", "hermes_cli.main", "gateway", "restart"], timeout=90)
        time.sleep(2)
    result["gateway_after"] = _gateway_snapshot()
    result["kanban_dispatcher"] = _kanban_dispatcher_snapshot()
    if worker_smoke:
        result["worker_smoke"] = run_worker_smoke()
    result["activation"] = {
        "code_identity_present": bool(result["diagnostics"].get("git_head") or result["diagnostics"].get("file_hashes")),
        "gateway_running": bool(result["gateway_after"].get("process", {}).get("running")),
        "dispatcher_ok": bool(result["kanban_dispatcher"].get("ok")),
        "direct_smoke_passed": bool(result["direct_smoke"].get("ok")),
        "worker_smoke_passed": None if not worker_smoke else bool(result.get("worker_smoke", {}).get("ok")),
    }
    if restart_gateway:
        before_pid = before_gateway.get("process", {}).get("pid") if isinstance(before_gateway, dict) else None
        after_pid = result["gateway_after"].get("process", {}).get("pid")
        result["activation"]["gateway_pid_changed_or_running_after_restart"] = bool(after_pid and after_pid != before_pid)
    result["ok"] = all(
        item.get("ok")
        for key, item in result.items()
        if key not in {"diagnostics", "gateway_before", "gateway_after", "activation"} and isinstance(item, dict)
    ) and bool(result["activation"]["code_identity_present"] and result["activation"]["direct_smoke_passed"])
    return result


def repair(*, restart_gateway: bool = False, worker_smoke: bool = False) -> dict[str, Any]:
    """Run the deterministic break-glass repair/verification sequence."""
    result: dict[str, Any] = {
        "diagnostics": collect_diagnostics(),
        "py_compile": run_py_compile(),
        "targeted_tests": run_targeted_tests(),
        "direct_smoke": run_direct_tool_smoke(),
    }
    if restart_gateway:
        result["gateway_restart"] = _run([_python(), "-m", "hermes_cli.main", "gateway", "restart"], timeout=90)
        time.sleep(2)
        result["gateway_status_after_restart"] = _run([_python(), "-m", "hermes_cli.main", "gateway", "status"], timeout=30)
    if worker_smoke:
        result["worker_smoke"] = run_worker_smoke()
    result["ok"] = all(
        item.get("ok")
        for key, item in result.items()
        if key != "diagnostics" and isinstance(item, dict)
    )
    if not result["ok"]:
        failures = []
        for key, item in result.items():
            if isinstance(item, dict) and not item.get("ok", True):
                failures.append({"step": key, "failure_class": item.get("failure_class"), "output_tail": item.get("output_tail", "")[-1000:]})
        result["failures"] = failures
        result["next_action"] = "Fix the classified deterministic failure first; only then optionally use Codex/Claude as executor."
    return result


def _emit(payload: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"ok: {payload.get('ok')}")
        for key, value in payload.items():
            if key == "ok":
                continue
            if isinstance(value, dict):
                status = value.get("ok")
                fc = value.get("failure_class")
                suffix = f" ({fc})" if fc else ""
                print(f"{key}: {status}{suffix}")
            else:
                print(f"{key}: {value}")
    return 0 if payload.get("ok", False) else 1


def cmd_break_glass(args: argparse.Namespace) -> int:
    action = getattr(args, "break_glass_action", None) or "diagnose"
    if action == "diagnose":
        return _emit(collect_diagnostics(), as_json=args.json)
    if action == "smoke":
        payload = run_worker_smoke(profile=args.profile, timeout=args.timeout) if args.worker else run_direct_tool_smoke()
        return _emit(payload, as_json=args.json)
    if action == "test":
        payload = {"ok": True, "py_compile": run_py_compile(), "targeted_tests": run_targeted_tests()}
        payload["ok"] = payload["py_compile"]["ok"] and payload["targeted_tests"]["ok"]
        return _emit(payload, as_json=args.json)
    if action == "verify":
        return _emit(verify_activation(restart_gateway=args.restart_gateway, worker_smoke=args.worker_smoke), as_json=args.json)
    if action == "repair":
        return _emit(repair(restart_gateway=args.restart_gateway, worker_smoke=args.worker_smoke), as_json=args.json)
    raise SystemExit(f"unknown break-glass action: {action}")


BREAK_GLASS_HELP = "Local self-repair path for Hermes/Kanban tool-runtime failures"
BREAK_GLASS_DESCRIPTION = (
    "Deterministic local diagnostics/smoke/repair for cases where the normal "
    "model tool-dispatch or Kanban worker path is broken. Does not require "
    "handle_function_call() for orchestration."
)


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate an already-created ``break-glass`` parser with its action
    subparsers and default handler.

    Takes the parser the caller already created — the plugin CLI dispatch in
    ``hermes_cli/main.py`` does ``subparsers.add_parser("break-glass", ...)`` and
    hands the result here as the ``register_cli_command`` ``setup_fn``. Kept
    separate from parser *creation* so one body satisfies that contract; the
    command itself now lives in the ``break-glass-cli`` plugin rather than an
    inline edit in ``main.py`` (see docs/LOCAL_PATCHES.md #14).
    """
    sub = parser.add_subparsers(dest="break_glass_action")

    p_diag = sub.add_parser("diagnose", help="Collect safe local diagnostics")
    p_diag.add_argument("--json", action="store_true")

    p_smoke = sub.add_parser("smoke", help="Run Kanban/tool-runtime smoke")
    p_smoke.add_argument("--json", action="store_true")
    p_smoke.add_argument("--worker", action="store_true", help="Run an opt-in LLM worker smoke instead of direct deterministic smoke")
    p_smoke.add_argument("--profile", default="worker", help="Worker profile for --worker (default: worker)")
    p_smoke.add_argument("--timeout", type=int, default=180, help="Worker smoke timeout seconds")

    p_test = sub.add_parser("test", help="Run break-glass py_compile + targeted regression tests")
    p_test.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify activation after a runtime patch")
    p_verify.add_argument("--json", action="store_true")
    p_verify.add_argument("--restart-gateway", action="store_true", help="Explicitly restart gateway before final status check")
    p_verify.add_argument("--worker-smoke", action="store_true", help="Also run opt-in LLM worker smoke")

    p_repair = sub.add_parser("repair", help="Run deterministic repair/verification sequence")
    p_repair.add_argument("--json", action="store_true")
    p_repair.add_argument("--restart-gateway", action="store_true", help="Explicitly restart gateway after tests/smoke")
    p_repair.add_argument("--worker-smoke", action="store_true", help="Also run opt-in LLM worker smoke")

    parser.set_defaults(func=cmd_break_glass)
    return parser


def build_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Back-compat shim: create the ``break-glass`` subparser and configure it.

    No longer wired from ``main.py`` (the command moved to the ``break-glass-cli``
    plugin). Retained so any direct caller / test keeps working.
    """
    parser = subparsers.add_parser(
        "break-glass", help=BREAK_GLASS_HELP, description=BREAK_GLASS_DESCRIPTION
    )
    return configure_parser(parser)
