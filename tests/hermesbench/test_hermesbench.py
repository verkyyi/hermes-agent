"""Tests for the HermesBench consolidated benchmark harness.

Deterministic — exercises the registry, scoring normalization, store round-trip,
report deltas, and the runner's error/skip handling without any LLM calls. The
model-backed suites self-skip when HERMES_RUN_LLM_EVALS is unset and are never
invoked here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evals.hermesbench import registry, report as report_mod, run as run_mod, store


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_ids_unique_and_valid():
    suites = registry.all_suites()
    ids = [s.id for s in suites]
    assert len(ids) == len(set(ids)), "duplicate suite ids"
    for s in suites:
        assert s.mode in (registry.AUTOMATED, registry.LLM_JUDGE, registry.HYBRID)
        assert s.weight > 0


def test_select_all_by_default():
    assert {s.id for s in registry.select()} == {
        "responsiveness", "kanban_scale", "orchestrator", "origin_return",
    }


def test_select_by_id():
    sel = registry.select(ids=["responsiveness"])
    assert [s.id for s in sel] == ["responsiveness"]


def test_suite_runners_importable():
    # Every declared runner must resolve to a callable (catches typos/renames).
    for s in registry.all_suites():
        assert callable(s.load())


# --------------------------------------------------------------------------- #
# runner: scoring + error/skip handling
# --------------------------------------------------------------------------- #
class _FakeSuite:
    """Stand-in for registry.Suite (which is frozen) with a settable result."""

    def __init__(self, sid, result=None, weight=1.0, raises=None):
        self.id = sid
        self.category = "c"
        self.mode = registry.AUTOMATED
        self.weight = weight
        self.summary = ""
        self._result = result
        self._raises = raises

    def load(self):
        def _fn():
            if self._raises:
                raise self._raises
            return self._result
        return _fn


def test_execute_passes_through_suite_skip():
    # A suite that self-skips (e.g. a model suite without HERMES_RUN_LLM_EVALS)
    # is recorded as skipped, not failed, with no score.
    s = _FakeSuite("orchestrator", result={"skipped": True, "skip_reason": "no creds"})
    res = run_mod._execute(s)
    assert res["skipped"] is True
    assert res["skip_reason"] == "no creds"
    assert res["score"] is None


def test_execute_captures_suite_error():
    s = _FakeSuite("boom", raises=RuntimeError("kaboom"))
    res = run_mod._execute(s)
    assert res["error"] and "kaboom" in res["error"]
    assert res["passed"] is None


def test_overall_score_is_weighted_over_ran_suites(monkeypatch):
    suites = [
        _FakeSuite("a", result={"score": 100.0, "passed": True}, weight=1.0),
        _FakeSuite("b", result={"score": 60.0, "passed": True}, weight=3.0),
    ]
    monkeypatch.setattr(registry, "select", lambda **kw: suites)
    rep = run_mod.run_benchmark()
    # weighted: (1*100 + 3*60) / 4 = 70
    assert rep["overall_score"] == pytest.approx(70.0)
    assert rep["passed"] is True
    assert rep["suites_ran"] == 2


def test_run_fails_when_a_ran_suite_fails(monkeypatch):
    suites = [_FakeSuite("a", result={"score": 10.0, "passed": False})]
    monkeypatch.setattr(registry, "select", lambda **kw: suites)
    rep = run_mod.run_benchmark()
    assert rep["passed"] is False


def test_skipped_suites_do_not_fail_the_run(monkeypatch):
    suites = [_FakeSuite("a", result={"skipped": True, "skip_reason": "nope"})]
    monkeypatch.setattr(registry, "select", lambda **kw: suites)
    rep = run_mod.run_benchmark()
    assert rep["passed"] is True
    assert rep["overall_score"] is None
    assert rep["suites_ran"] == 0


# --------------------------------------------------------------------------- #
# store round-trip + report deltas
# --------------------------------------------------------------------------- #
def _mk_report(run_id, ts, overall, suite_score):
    suite = {
        "id": "responsiveness", "category": "Front-desk", "mode": "automated",
        "score": suite_score, "passed": True, "skipped": False,
        "skip_reason": None, "error": None, "duration_s": 0.1,
        "metrics": {"ack_accuracy": 0.9},
    }
    return {
        "run_id": run_id, "ts": ts, "overall_score": overall,
        "passed": True, "suites_ran": 1,
        "harness": {"git_sha": "abc123", "model_id": "gpt-5.5", "profile_hash": "deadbeef"},
        "suites": [suite],
    }


def test_store_round_trip_and_previous():
    db = Path(tempfile.mkdtemp()) / "hb.db"
    store.save_run(_mk_report("hb-1", "2026-05-28T00:00:00+00:00", 90.0, 90.0), db)
    store.save_run(_mk_report("hb-2", "2026-05-28T01:00:00+00:00", 85.5, 85.5), db)
    runs = store.recent_runs(db_path=db)
    assert [r["run_id"] for r in runs] == ["hb-2", "hb-1"]  # newest first
    assert runs[0]["suites"][0]["id"] == "responsiveness"
    assert runs[0]["suites"][0]["metrics"]["ack_accuracy"] == 0.9
    prev = store.previous_run("hb-2", db_path=db)
    assert prev and prev["run_id"] == "hb-1"


def test_store_recent_runs_empty_when_no_db():
    db = Path(tempfile.mkdtemp()) / "missing.db"
    assert store.recent_runs(db_path=db) == []


def test_report_renders_delta():
    prev = _mk_report("hb-1", "2026-05-28T00:00:00+00:00", 90.0, 90.0)
    cur = _mk_report("hb-2", "2026-05-28T01:00:00+00:00", 85.5, 85.5)
    out = report_mod.render(cur, prev)
    assert "OVERALL" in out and "85.5" in out
    assert "(-4.5)" in out  # delta vs previous
    assert "PASS" in out


def test_report_handles_no_previous():
    cur = _mk_report("hb-1", "2026-05-28T00:00:00+00:00", 90.0, 90.0)
    out = report_mod.render(cur, None)
    assert "90.0" in out  # renders without a delta and doesn't crash
