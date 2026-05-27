"""Responsiveness rubric — pure functions over the real gateway policies.

Everything here drives the SAME decision functions the live gateway uses
(gateway.run._pre_llm_ack_eligible_source, _should_send_pre_llm_ack,
_should_send_public_progress, _public_progress_phase) so the score tracks
production behavior, not a reimplementation.

Perceived responsiveness is modeled as *time to first visible feedback* (TTFF):

  acked turn .................. ACK_LATENCY_S       (gateway sends before the model call)
  un-acked trivial/short ...... SHORT_REPLY_LATENCY_S (model answers directly, fast)
  un-acked long_work .......... first "still working" notice at the progress
                                initial delay, else the full run if none fires
                                (the user stares at silence the whole turn —
                                the exact failure the pre-LLM ack exists to fix)

The latency constants are the only modeled numbers; they are env-overridable so
the model can be re-pegged to measured production latencies without code edits.
"""

from __future__ import annotations

import os
from typing import Any

from gateway.run import (
    _PUBLIC_LONG_PROGRESS_DEFAULT_INITIAL_DELAY_S as DEFAULT_INITIAL_DELAY_S,
    _PUBLIC_LONG_PROGRESS_LONG_SILENCE_S as DEFAULT_LONG_SILENCE_S,
    _PUBLIC_LONG_PROGRESS_MIN_INTERVAL_S as DEFAULT_MIN_INTERVAL_S,
    _pre_llm_ack_eligible_source,
    _should_send_pre_llm_ack,
    _should_send_public_progress,
)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Latency model (seconds). See module docstring.
ACK_LATENCY_S = _float_env("HERMES_RESP_ACK_LATENCY_S", 0.3)
SHORT_REPLY_LATENCY_S = _float_env("HERMES_RESP_SHORT_REPLY_LATENCY_S", 6.0)
PROGRESS_TICK_S = _float_env("HERMES_RESP_PROGRESS_TICK_S", 10.0)

# Pass thresholds for the GREEN (already-shipping) slice of the dataset.
THRESHOLDS = {
    "ack_accuracy": 1.0,            # every GREEN turn's ack decision must be correct
    "false_ack_rate": 0.0,          # never ack a trivial/short/command turn
    "long_work_p95_ttff_s": ACK_LATENCY_S + 0.001,  # acked long turns: sub-second feedback
    "cadence_ok_rate": 1.0,         # progress cadence correct for every long turn
}


def predict_ack(turn: dict, *, platform: str) -> tuple[bool, dict]:
    """Replay the gateway's pre-LLM ack decision for one turn."""
    eligible, eligible_reason = _pre_llm_ack_eligible_source(platform, turn["source"])
    should, text_reason = _should_send_pre_llm_ack(
        turn["text"], is_command=turn.get("is_command", False)
    )
    ack = bool(eligible and should)
    return ack, {
        "eligible": eligible,
        "eligible_reason": eligible_reason,
        "heuristic": should,
        "heuristic_reason": text_reason,
    }


def time_to_first_feedback(
    turn: dict,
    ack: bool,
    *,
    platform: str,
    initial_delay_s: float = DEFAULT_INITIAL_DELAY_S,
) -> float:
    """Model seconds until the user sees ANY feedback for this turn."""
    if ack:
        return ACK_LATENCY_S
    if turn["kind"] in ("trivial", "short", "command"):
        return SHORT_REPLY_LATENCY_S
    # long_work with no ack: silent until the first public-progress notice (if
    # the surface supports it and the turn runs long enough), else the whole run.
    run = float(turn.get("run_seconds", 0) or 0)
    supported, _ = _should_send_public_progress(
        platform=platform,
        profile_name=None,
        hermes_home=None,
        elapsed_s=initial_delay_s,
        now_s=initial_delay_s,
        last_sent_s=None,
        last_phase=None,
        phase="working",
        initial_delay_s=initial_delay_s,
    )
    if supported and run >= initial_delay_s:
        return float(initial_delay_s)
    return run


def _phase_at(phases: list, sec: float) -> str:
    current = phases[0][1]
    for at, phase in phases:
        if at <= sec:
            current = phase
    return current


def simulate_progress_notices(
    turn: dict,
    *,
    platform: str,
    profile_name: str | None,
    hermes_home: str | None,
    tick_s: float = PROGRESS_TICK_S,
    initial_delay_s: float = DEFAULT_INITIAL_DELAY_S,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    long_silence_s: float = DEFAULT_LONG_SILENCE_S,
) -> list[dict]:
    """Step the gateway's public-progress loop across the turn's lifetime.

    Returns the notices that would fire, each {at, phase, reason}. Uses one
    clock for both elapsed and wall time (the real loop's since-last math is a
    delta, so a shared clock is faithful).
    """
    run = float(turn.get("run_seconds", 0) or 0)
    phases = turn.get("phases") or [[0, "working"]]
    notices: list[dict] = []
    last_sent: float | None = None
    last_phase: str | None = None
    t = 0.0
    while t <= run + 1e-9:
        phase = _phase_at(phases, t)
        allowed, reason = _should_send_public_progress(
            platform=platform,
            profile_name=profile_name,
            hermes_home=hermes_home,
            elapsed_s=t,
            now_s=t,
            last_sent_s=last_sent,
            last_phase=last_phase,
            phase=phase,
            initial_delay_s=initial_delay_s,
            min_interval_s=min_interval_s,
            long_silence_s=long_silence_s,
        )
        if allowed:
            notices.append({"at": t, "phase": phase, "reason": reason})
            last_sent = t
            last_phase = phase
        t += tick_s
    return notices


def score_turn(
    turn: dict,
    *,
    platform: str,
    profile_name: str | None,
    hermes_home: str | None,
    tick_s: float = PROGRESS_TICK_S,
    initial_delay_s: float = DEFAULT_INITIAL_DELAY_S,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
) -> dict:
    """Grade one turn. Returns ack decision, TTFF, and cadence components."""
    ack, ack_info = predict_ack(turn, platform=platform)
    expected = bool(turn["expect_ack"])
    ttff = time_to_first_feedback(turn, ack, platform=platform, initial_delay_s=initial_delay_s)
    ttff_no_ack = time_to_first_feedback(turn, False, platform=platform, initial_delay_s=initial_delay_s)

    row: dict[str, Any] = {
        "id": turn["id"],
        "session": turn.get("session"),
        "kind": turn["kind"],
        "source": turn["source"],
        "known_gap": turn.get("known_gap", False),
        "expect_ack": expected,
        "ack": ack,
        "ack_correct": ack == expected,
        "ack_info": ack_info,
        "ttff_s": ttff,
        "ttff_no_ack_s": ttff_no_ack,
    }

    # Cadence only applies to long-running turns (anything that can outlast the
    # progress initial delay). Skip casual short/trivial/command turns.
    if turn["kind"] == "long_work":
        run = float(turn.get("run_seconds", 0) or 0)
        notices = simulate_progress_notices(
            turn,
            platform=platform,
            profile_name=profile_name,
            hermes_home=hermes_home,
            tick_s=tick_s,
            initial_delay_s=initial_delay_s,
            min_interval_s=min_interval_s,
        )
        ats = [n["at"] for n in notices]
        gaps = [b - a for a, b in zip(ats, ats[1:])]
        expect_notice = run >= initial_delay_s
        first_timely = (
            (not notices and not expect_notice)
            or (bool(notices) and initial_delay_s <= ats[0] <= initial_delay_s + tick_s)
        )
        gaps_ok = all(g >= min_interval_s - 1e-9 for g in gaps)
        presence_ok = (len(notices) >= 1) == expect_notice
        row["cadence"] = {
            "run_seconds": run,
            "num_notices": len(notices),
            "first_notice_at": ats[0] if ats else None,
            "expect_notice": expect_notice,
            "first_timely": first_timely,
            "gaps_ok": gaps_ok,
            "presence_ok": presence_ok,
            "ok": bool(first_timely and gaps_ok and presence_ok),
            "notices": notices,
        }
    return row


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def score_dataset(
    turns: list[dict],
    *,
    platform: str,
    profile_name: str | None,
    hermes_home: str | None,
) -> dict:
    """Aggregate the benchmark. GREEN turns gate pass/fail; gap turns report the target."""
    rows = [
        score_turn(t, platform=platform, profile_name=profile_name, hermes_home=hermes_home)
        for t in turns
    ]
    green = [r for r in rows if not r["known_gap"]]
    gaps = [r for r in rows if r["known_gap"]]

    # The TTFF headline measures the ack's win on turns that SHOULD be acked
    # (eligible DM long-work). Group long-work is validated separately — it must
    # NOT ack, and its progress cadence carries its responsiveness — so it does
    # not belong in this set, where it would masquerade as an ack regression.
    green_long = [r for r in green if r["kind"] == "long_work" and r["expect_ack"]]
    long_all = [r for r in rows if r["kind"] == "long_work"]

    # Ack quality over the GREEN slice.
    should_ack = [r for r in green if r["expect_ack"]]
    should_not = [r for r in green if not r["expect_ack"]]
    ack_recall = (sum(1 for r in should_ack if r["ack"]) / len(should_ack)) if should_ack else 1.0
    false_ack_rate = (sum(1 for r in should_not if r["ack"]) / len(should_not)) if should_not else 0.0
    ack_accuracy = (sum(1 for r in green if r["ack_correct"]) / len(green)) if green else 1.0

    # TTFF headline over GREEN long-work turns (the win the ack delivers).
    long_ttff = [r["ttff_s"] for r in green_long]
    long_ttff_no_ack = [r["ttff_no_ack_s"] for r in green_long]

    # Cadence over every long-work turn that runs the progress loop.
    cadence_rows = [r for r in long_all if "cadence" in r]
    cadence_ok_rate = (
        sum(1 for r in cadence_rows if r["cadence"]["ok"]) / len(cadence_rows)
        if cadence_rows else 1.0
    )

    metrics = {
        "turns_total": len(rows),
        "green_turns": len(green),
        "gap_turns": len(gaps),
        "ack_accuracy": ack_accuracy,
        "ack_recall": ack_recall,
        "false_ack_rate": false_ack_rate,
        "long_work_mean_ttff_s": (sum(long_ttff) / len(long_ttff)) if long_ttff else 0.0,
        "long_work_p95_ttff_s": _p95(long_ttff),
        "long_work_mean_ttff_no_ack_s": (
            sum(long_ttff_no_ack) / len(long_ttff_no_ack) if long_ttff_no_ack else 0.0
        ),
        "cadence_ok_rate": cadence_ok_rate,
        # Improvement target: gap turns the heuristic would need to start acking.
        "gap_turns_acked": sum(1 for r in gaps if r["ack"]),
    }

    passed = (
        metrics["ack_accuracy"] >= THRESHOLDS["ack_accuracy"]
        and metrics["false_ack_rate"] <= THRESHOLDS["false_ack_rate"]
        and metrics["long_work_p95_ttff_s"] <= THRESHOLDS["long_work_p95_ttff_s"]
        and metrics["cadence_ok_rate"] >= THRESHOLDS["cadence_ok_rate"]
    )
    return {"passed": passed, "metrics": metrics, "rows": rows, "thresholds": THRESHOLDS}
