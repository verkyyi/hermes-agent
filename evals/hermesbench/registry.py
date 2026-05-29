"""Suite registry for HermesBench.

Each suite is metadata + a lazily-imported ``run`` callable. The callable takes
no args and returns a dict with at least::

    {"score": float (0..100), "passed": bool, "metrics": dict}

and may optionally return ``{"skipped": True, "skip_reason": str}``. Suites that
need a model / credentials (orchestrator, origin_return) self-skip when
HERMES_RUN_LLM_EVALS is unset — so a creds-less run degrades cleanly without any
tier machinery. The runner (run.py) wraps timing and error capture around the
call; the suite functions themselves stay simple.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

# Grading modes, mirroring ClawBench Core v1. ``automated`` suites are
# deterministic (no model); ``hybrid`` / ``llm_judge`` suites drive a real
# model run and self-skip without HERMES_RUN_LLM_EVALS.
AUTOMATED = "automated"      # precise deterministic comparison
LLM_JUDGE = "llm_judge"      # frontier model judges the outcome
HYBRID = "hybrid"            # deterministic structure + judged quality


@dataclass(frozen=True)
class Suite:
    id: str
    category: str
    mode: str
    weight: float
    runner: str  # "module:function" — imported lazily at execution time
    summary: str = ""

    def load(self) -> Callable[[], dict]:
        mod_name, _, fn_name = self.runner.partition(":")
        mod = importlib.import_module(mod_name)
        return getattr(mod, fn_name)


# Order is the display order. Weights need not sum to 1 — the runner normalizes
# over whichever suites actually ran (skipped/errored suites drop out).
_SUITES: list[Suite] = [
    Suite(
        id="responsiveness",
        category="Front-desk responsiveness",
        mode=AUTOMATED,
        weight=1.0,
        runner="evals.hermesbench.suites.responsiveness:run",
        summary="Pre-LLM ack accuracy, false-ack rate, progress cadence (deterministic).",
    ),
    Suite(
        id="kanban_scale",
        category="Kanban kernel scale",
        mode=AUTOMATED,
        weight=0.7,
        runner="evals.hermesbench.suites.kanban_scale:run",
        summary="Dispatch / recompute / context / query latency at 100..10k tasks.",
    ),
    Suite(
        id="orchestrator",
        category="Orchestration & routing",
        mode=HYBRID,
        weight=1.0,
        runner="evals.hermesbench.suites.orchestrator:run",
        summary="Real orchestrator routing/linking accuracy over isolated boards.",
    ),
    Suite(
        id="origin_return",
        category="End-to-end task return",
        mode=LLM_JUDGE,
        weight=0.8,
        runner="evals.hermesbench.suites.origin_return:run",
        summary="Front-desk turn -> task -> completion returns to the originator.",
    ),
]


def all_suites() -> list[Suite]:
    return list(_SUITES)


def by_id(suite_id: str) -> Suite | None:
    return next((s for s in _SUITES if s.id == suite_id), None)


def select(*, ids: list[str] | None = None) -> list[Suite]:
    """Select suites for a run, optionally restricted to named ``ids``."""
    if not ids:
        return list(_SUITES)
    wanted = set(ids)
    return [s for s in _SUITES if s.id in wanted]
