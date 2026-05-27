"""Default-profile responsiveness benchmark — deterministic suite.

Verky's responsiveness benchmark (2026-05-27). Kept in its own directory,
separate from the upstream Hermes test tree, mirroring tests/orchestrator_benchmark/.

It drives the REAL gateway responsiveness policies (gateway.run) over emulated
user sessions (evals/responsiveness/dataset.py) and asserts the perceived-latency
contract for the front desk:

  Suite A — per-turn pre-LLM ack decision matches the ideal.        [GREEN]
  Suite B — casual long-work turns that should ack but don't yet.   [TDD]
  Suite C — aggregate thresholds + the ack-vs-silence TTFF win.     [GREEN]
  Suite D — public long-progress cadence is well-formed.            [GREEN]
  Suite E — group/eligibility gating + dataset sanity.              [GREEN]

Markers, matching the orchestrator benchmark convention:
  [GREEN] current shipping behavior — must pass now.
  [TDD]   forward-looking gap — xfail(strict=True). When the heuristic learns to
          ack these casual long-work turns the xpass flips the suite red, forcing
          the marker's removal (the "feature done" signal).

Fully deterministic: no live LLM, no kanban DB, no network — runs in CI.
"""

from __future__ import annotations

import pytest

from evals.responsiveness import score as S
from evals.responsiveness.dataset import (
    HERMES_HOME,
    PLATFORM,
    PROFILE_NAME,
    all_turns,
)

TURNS = all_turns()
GREEN = [t for t in TURNS if not t["known_gap"]]
GAPS = [t for t in TURNS if t["known_gap"]]
LONG_GREEN = [t for t in GREEN if t["kind"] == "long_work"]


def _report() -> dict:
    return S.score_dataset(
        TURNS, platform=PLATFORM, profile_name=PROFILE_NAME, hermes_home=HERMES_HOME
    )


# --- Suite A: per-turn ack decision -----------------------------------------
@pytest.mark.parametrize("turn", GREEN, ids=[t["id"] for t in GREEN])
def test_ack_decision_matches_ideal_green(turn):
    """[GREEN] Each shipping turn acks (or stays silent) exactly as intended."""
    ack, info = S.predict_ack(turn, platform=PLATFORM)
    assert ack == turn["expect_ack"], info


# --- Suite B: forward-looking gaps ------------------------------------------
@pytest.mark.parametrize(
    "turn",
    [
        pytest.param(
            t, marks=pytest.mark.xfail(strict=True, reason="casual long-work not yet acked")
        )
        for t in GAPS
    ],
    ids=[t["id"] for t in GAPS],
)
def test_ack_decision_matches_ideal_gap(turn):
    """[TDD] Casual long-work turns *should* ack; today's heuristic misses them."""
    ack, info = S.predict_ack(turn, platform=PLATFORM)
    assert ack == turn["expect_ack"], info


# --- Suite C: aggregate thresholds + the responsiveness win -----------------
def test_ack_accuracy_perfect_on_green():
    """[GREEN] Every GREEN turn's ack decision is correct."""
    m = _report()["metrics"]
    assert m["ack_accuracy"] >= S.THRESHOLDS["ack_accuracy"], m


def test_no_false_acks_on_casual_turns():
    """[GREEN] Trivial/short/command turns are never pre-acked (no spam)."""
    m = _report()["metrics"]
    assert m["false_ack_rate"] <= S.THRESHOLDS["false_ack_rate"], m


def test_long_work_first_feedback_is_subsecond():
    """[GREEN] On long-work turns the ack delivers sub-second first feedback."""
    m = _report()["metrics"]
    assert m["long_work_p95_ttff_s"] <= S.THRESHOLDS["long_work_p95_ttff_s"], m


def test_ack_beats_no_ack_baseline_by_a_wide_margin():
    """[GREEN] The whole point: acking collapses TTFF versus dead silence."""
    m = _report()["metrics"]
    assert m["long_work_mean_ttff_s"] < m["long_work_mean_ttff_no_ack_s"]
    assert m["long_work_mean_ttff_s"] * 10 <= m["long_work_mean_ttff_no_ack_s"]


def test_benchmark_passes_overall():
    """[GREEN] Aggregate gate over every threshold."""
    report = _report()
    assert report["passed"], report["metrics"]


# --- Suite D: public progress cadence ---------------------------------------
@pytest.mark.parametrize("turn", LONG_GREEN, ids=[t["id"] for t in LONG_GREEN])
def test_progress_cadence_is_well_formed(turn):
    """[GREEN] During a long turn the first 'still working' notice is timely and
    re-notices never arrive faster than the min interval."""
    row = S.score_turn(
        turn, platform=PLATFORM, profile_name=PROFILE_NAME, hermes_home=HERMES_HOME
    )
    assert row["cadence"]["ok"], row["cadence"]


def test_cadence_ok_rate_perfect():
    """[GREEN] Cadence holds for every long-work turn in the dataset."""
    m = _report()["metrics"]
    assert m["cadence_ok_rate"] >= S.THRESHOLDS["cadence_ok_rate"], m


# --- Suite E: eligibility gating + dataset sanity ---------------------------
def test_group_long_work_is_gated_not_acked():
    """[GREEN] A long-work turn in a GROUP must not pre-ack (group discipline),
    yet still gets public-progress notices as its responsiveness channel."""
    grp = next(t for t in TURNS if t["source"] == "group")
    ack, info = S.predict_ack(grp, platform=PLATFORM)
    assert ack is False
    assert info["eligible_reason"] == "not_dm"
    row = S.score_turn(
        grp, platform=PLATFORM, profile_name=PROFILE_NAME, hermes_home=HERMES_HOME
    )
    assert row["cadence"]["num_notices"] >= 1, row["cadence"]


def test_dataset_is_well_formed():
    """[GREEN] Every turn carries the fields the scorer relies on."""
    assert TURNS, "dataset is empty"
    for t in TURNS:
        assert t["kind"] in {"trivial", "short", "long_work", "command"}, t
        assert t["source"] in {"dm", "group"}, t
        assert isinstance(t["expect_ack"], bool), t
        if t["kind"] == "long_work":
            assert t.get("run_seconds"), f"long_work turn needs run_seconds: {t['id']}"
        # Only long-work turns are ever an ideal ack target.
        if t["expect_ack"]:
            assert t["kind"] == "long_work", t
