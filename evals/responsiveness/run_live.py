"""Live time-to-first-token (TTFT) mode for the responsiveness benchmark.

OPT-IN — invokes the REAL default-profile agent on each emulated user request
and reads the model's *measured* latency from telemetry (agent.telemetry's
``turns`` table: ``ttft_ms`` first token, ``ttfa_ms`` first answer/ack,
``ttlt_ms`` total). This is the answer to "did we actually measure TTFT on
daily requests?" — run it and you get real numbers.

Tool modes (``--tools``):
  full   THE DEFAULT — the profile's real configured toolset (web, calendar,
         mail, etc.) and real credentials, for the most realistic end-to-end
         result. Because real tools have side effects, it runs ONLY the turns
         marked tool_safe; the destructive turns (tool_safe=False) require BOTH
         --include-unsafe AND --allow-side-effects (or HERMES_RESP_ALLOW_SIDE_EFFECTS=1).
  safe   read-only toolset allowlist (web search/extract, vision). Realistic for
         "look something up" requests and reflects real tool round-trips in
         TTLT, but cannot write files, send mail, run shell, etc.
  none   text-only — a no-op toolset (0 tools). Pure model first-token latency,
         zero side effects. Use to isolate the model from tool-round-trip noise.

Isolation & parallelism:
  * Each (turn, trial) job runs in its OWN temp HERMES_HOME (config.yaml + .env
    + context_length_cache.yaml copied from the real default profile). One job
    per home means one telemetry row per DB — no attribution ambiguity — which
    is also what makes --concurrency safe.
  * --concurrency N runs N jobs at once (default from HERMES_RESP_LIVE_CONCURRENCY,
    currently 8). CAVEAT: this is a *latency* benchmark; concurrent LLM calls
    contend for provider capacity and can inflate TTFT, so a big concurrency
    trades fidelity for wall-clock speed. For the most ACCURATE number use
    --concurrency 1 (one request at a time, like a real user session) with
    several --trials. High concurrency may also hit provider rate limits.

Gated behind HERMES_RUN_LLM_EVALS=1 (real LLM calls cost money / hit the
network), mirroring evals/orchestrator_routing.

    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.responsiveness.run_live
    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.responsiveness.run_live --only-daily --trials 3 --concurrency 4
    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m evals.responsiveness.run_live --tools full --allow-side-effects
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from evals.responsiveness.dataset import all_turns

DAILY_KINDS = {"trivial", "short", "command"}
DEFAULT_TIMEOUT_S = int(os.environ.get("HERMES_RESP_LIVE_TIMEOUT_S", "120"))
# Bigger default fan-out for wall-clock speed; override with the env var or
# --concurrency. NOTE: this inflates absolute TTFT (concurrent calls contend),
# so the *most accurate* number still comes from --concurrency 1 + more trials.
DEFAULT_CONCURRENCY = int(os.environ.get("HERMES_RESP_LIVE_CONCURRENCY", "8"))

# Tool-mode -> the --toolsets argument to pass (None = use the profile default).
NOOP_TOOLSET = "__none__"          # resolves to 0 tools
SAFE_TOOLSETS = "web,search,vision"  # read-only: no writes/sends/shell
TOOL_MODES = ("none", "safe", "full")


def _toolsets_arg(tools_mode: str) -> str | None:
    if tools_mode == "none":
        return NOOP_TOOLSET
    if tools_mode == "safe":
        return SAFE_TOOLSETS
    return None  # full: let the profile's configured toolset apply


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
    """Temp HERMES_HOME carrying only the default profile's config + creds."""
    home = Path(tempfile.mkdtemp(prefix="resp-ttft-"))
    # context_length_cache.yaml avoids a per-home context-length probe on the
    # first model call, which would otherwise add latency + variance to TTFT.
    for name in ("config.yaml", ".env", "context_length_cache.yaml"):
        s = src_home / name
        if s.exists():
            shutil.copy2(s, home / name)
            try:
                (home / name).chmod(0o600)
            except OSError:
                pass
    return home


def _run_turn(text: str, *, home: Path, toolsets_arg: str | None, timeout_s: int) -> tuple[int, str]:
    """Spawn one default-profile turn. Returns (returncode, stderr_tail)."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_PROFILE"] = "default"
    env.pop("HERMES_KANBAN_TASK", None)
    cmd = [*_hermes_argv(), "chat", "-q", text, "--quiet"]
    if toolsets_arg is not None:
        cmd.extend(["--toolsets", toolsets_arg])
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout_s
        )
        return proc.returncode, (proc.stderr or "")[-600:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _read_turn_row(home: Path) -> dict | None:
    """The single newest telemetry turn row for this isolated home."""
    db = home / "telemetry.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ttft_ms, ttfa_ms, ttlt_ms, tool_count, model, "
            "turn_class, output_chars, status FROM turns "
            "ORDER BY started_at_ms DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _run_one_job(turn: dict, trial: int, *, src_home: Path, toolsets_arg: str | None,
                 timeout_s: int) -> dict:
    """Run a single (turn, trial) in its own isolated home; return measured row."""
    home = _make_isolated_home(src_home)
    try:
        wall0 = time.monotonic()
        rc, stderr_tail = _run_turn(
            turn["text"], home=home, toolsets_arg=toolsets_arg, timeout_s=timeout_s
        )
        wall_ms = (time.monotonic() - wall0) * 1000.0
        row = _read_turn_row(home)
        return {
            "id": turn["id"],
            "kind": turn["kind"],
            "source": turn["source"],
            "trial": trial,
            "returncode": rc,
            "wall_ms": round(wall_ms, 1),
            "ttft_ms": (row or {}).get("ttft_ms"),
            "ttfa_ms": (row or {}).get("ttfa_ms"),
            "ttlt_ms": (row or {}).get("ttlt_ms"),
            "tool_count": (row or {}).get("tool_count"),
            "model": (row or {}).get("model"),
            "telemetry_status": (row or {}).get("status"),
            "error": None if row else (stderr_tail or "no telemetry row"),
        }
    finally:
        shutil.rmtree(home, ignore_errors=True)


def measure(turns: list[dict], *, trials: int = 1, timeout_s: int = DEFAULT_TIMEOUT_S,
            tools_mode: str = "none", concurrency: int = DEFAULT_CONCURRENCY) -> list[dict]:
    """Run each turn `trials` times; collect measured telemetry.

    concurrency>1 fans jobs across a thread pool (each job blocks on its own
    subprocess). Every job has its own isolated home, so parallelism never
    interleaves telemetry rows.
    """
    toolsets_arg = _toolsets_arg(tools_mode)
    src_home = _default_home()
    jobs = [(turn, trial) for trial in range(trials) for turn in turns]
    concurrency = max(1, int(concurrency))
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [
            pool.submit(_run_one_job, turn, trial, src_home=src_home,
                        toolsets_arg=toolsets_arg, timeout_s=timeout_s)
            for (turn, trial) in jobs
        ]
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: (r["id"], r["trial"]))
    return results


def _p(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
    return vals[idx]


def aggregate(results: list[dict]) -> dict:
    """Per-kind measured TTFT/TTLT summary."""
    by_kind: dict[str, dict] = {}
    for kind in sorted({r["kind"] for r in results}):
        rs = [r for r in results if r["kind"] == kind]
        ttft = [r["ttft_ms"] for r in rs if r["ttft_ms"] is not None]
        ttlt = [r["ttlt_ms"] for r in rs if r["ttlt_ms"] is not None]
        tools = [r["tool_count"] for r in rs if r["tool_count"] is not None]
        by_kind[kind] = {
            "n": len(rs),
            "n_measured": len(ttft),
            "ttft_p50_ms": _p(ttft, 0.50),
            "ttft_p95_ms": _p(ttft, 0.95),
            "ttlt_mean_ms": round(sum(ttlt) / len(ttlt), 1) if ttlt else None,
            "tool_calls_mean": round(sum(tools) / len(tools), 2) if tools else None,
        }
    daily = [r["ttft_ms"] for r in results
             if r["kind"] in DAILY_KINDS and r["ttft_ms"] is not None]
    return {
        "by_kind": by_kind,
        "daily_ttft_p50_ms": _p(daily, 0.50),
        "daily_ttft_p95_ms": _p(daily, 0.95),
        "failures": [r for r in results if r["ttft_ms"] is None],
    }


def select_turns(*, only_daily: bool = False, kinds: set[str] | None = None,
                 tools_mode: str = "none", include_unsafe: bool = False) -> list[dict]:
    turns = all_turns()
    if only_daily:
        kinds = DAILY_KINDS
    if kinds:
        turns = [t for t in turns if t["kind"] in kinds]
    # Under real tools, skip turns with side effects unless explicitly included.
    if tools_mode == "full" and not include_unsafe:
        turns = [t for t in turns if t.get("tool_safe")]
    return turns


def run_live(*, only_daily: bool = False, kinds: set[str] | None = None,
             trials: int = 1, timeout_s: int = DEFAULT_TIMEOUT_S,
             tools_mode: str = "full", concurrency: int = DEFAULT_CONCURRENCY,
             include_unsafe: bool = False) -> dict:
    turns = select_turns(only_daily=only_daily, kinds=kinds,
                          tools_mode=tools_mode, include_unsafe=include_unsafe)
    results = measure(turns, trials=trials, timeout_s=timeout_s,
                      tools_mode=tools_mode, concurrency=concurrency)
    return {"results": results, "summary": aggregate(results),
            "trials": trials, "turns": len(turns), "tools_mode": tools_mode,
            "concurrency": concurrency}


def _fmt_ms(v: float | None) -> str:
    return "  n/a " if v is None else f"{v/1000.0:6.2f}s"


def _format(report: dict) -> str:
    s = report["summary"]
    lines = [
        "=== live TTFT — default profile (real model) ===",
        f"turns={report['turns']} trials={report['trials']} "
        f"tools={report['tools_mode']} concurrency={report['concurrency']}  "
        f"(isolated HERMES_HOME per job)",
        "",
        f"{'kind':<10} {'n':>3} {'meas':>5} {'ttft p50':>9} {'ttft p95':>9} "
        f"{'ttlt mean':>10} {'tools':>6}",
    ]
    for kind, m in s["by_kind"].items():
        tcm = m["tool_calls_mean"]
        tcm_s = "  n/a" if tcm is None else f"{tcm:>6.2f}"
        lines.append(
            f"{kind:<10} {m['n']:>3} {m['n_measured']:>5} "
            f"{_fmt_ms(m['ttft_p50_ms']):>9} {_fmt_ms(m['ttft_p95_ms']):>9} "
            f"{_fmt_ms(m['ttlt_mean_ms']):>10} {tcm_s:>6}"
        )
    lines += [
        "",
        f"DAILY requests TTFT:  p50={_fmt_ms(s['daily_ttft_p50_ms'])}  "
        f"p95={_fmt_ms(s['daily_ttft_p95_ms'])}",
    ]
    min_n = min((m["n_measured"] for m in s["by_kind"].values()), default=0)
    if min_n < 5:
        lines += [
            "",
            f"note: only {min_n} sample(s) for the smallest kind — p95 is just the "
            "worst single sample here, not a stable tail. Raise --trials (and use "
            "--concurrency 1) before trusting p95.",
        ]
    if report["concurrency"] > 1:
        lines.append("")
        lines.append("!! concurrency>1: concurrent LLM calls contend for provider "
                     "capacity — absolute TTFT may be inflated. Use concurrency=1 "
                     "for the faithful number.")
    lines += [
        "",
        "calibration hint: feed the measured daily p50 into the deterministic",
        "model via HERMES_RESP_SHORT_REPLY_LATENCY_S=<seconds> to peg its baseline.",
    ]
    if s["failures"]:
        lines.append("")
        lines.append(f"!! {len(s['failures'])} turn(s) produced no TTFT (creds/timeout?):")
        for f in s["failures"][:5]:
            lines.append(f"   {f['id']} rc={f['returncode']} status={f['telemetry_status']} {str(f['error'])[:80]}")
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> dict:
    opts = {"only_daily": "--only-daily" in argv, "as_json": "--json" in argv,
            "trials": 1, "tools_mode": "full", "concurrency": DEFAULT_CONCURRENCY,
            "include_unsafe": "--include-unsafe" in argv,
            "allow_side_effects": "--allow-side-effects" in argv}
    for i, a in enumerate(argv):
        if a == "--trials" and i + 1 < len(argv):
            opts["trials"] = max(1, int(argv[i + 1]))
        elif a == "--concurrency" and i + 1 < len(argv):
            opts["concurrency"] = max(1, int(argv[i + 1]))
        elif a == "--tools" and i + 1 < len(argv):
            opts["tools_mode"] = argv[i + 1]
    return opts


def main(argv: list[str]) -> int:
    if os.environ.get("HERMES_RUN_LLM_EVALS") != "1":
        print("live TTFT mode is gated. Re-run with HERMES_RUN_LLM_EVALS=1 "
              "(real LLM calls; needs provider credentials).", file=sys.stderr)
        return 2
    opts = _parse_args(argv)
    if opts["tools_mode"] not in TOOL_MODES:
        print(f"--tools must be one of {TOOL_MODES}", file=sys.stderr)
        return 2
    allow = opts["allow_side_effects"] or os.environ.get("HERMES_RESP_ALLOW_SIDE_EFFECTS") == "1"
    # The destructive turns (tool_safe=False: file writes, shell, mail, PRs) only
    # ever run under --include-unsafe, and that still needs explicit confirmation.
    if opts["tools_mode"] == "full" and opts["include_unsafe"] and not allow:
        print("--include-unsafe runs tool_safe=False turns with REAL tools "
              "(file writes, shell, mail, PRs). Confirm with --allow-side-effects "
              "(or export HERMES_RESP_ALLOW_SIDE_EFFECTS=1).", file=sys.stderr)
        return 2
    if opts["tools_mode"] == "full":
        print("NOTE: --tools full uses the profile's REAL toolset and REAL "
              "account credentials (web, calendar, mail, etc.). Running the "
              "tool_safe turn set only.", file=sys.stderr)
    report = run_live(
        only_daily=opts["only_daily"], trials=opts["trials"],
        tools_mode=opts["tools_mode"], concurrency=opts["concurrency"],
        include_unsafe=opts["include_unsafe"],
    )
    if opts["as_json"]:
        print(json.dumps(report, indent=2))
    else:
        print(_format(report))
    measured_any = any(r["ttft_ms"] is not None for r in report["results"])
    return 0 if measured_any else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
