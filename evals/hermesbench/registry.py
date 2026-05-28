"""Suite registry for HermesBench.

Each suite is metadata + a lazily-imported ``run`` callable. The callable takes
no args and returns a dict with at least::

    {"score": float (0..100), "passed": bool, "metrics": dict}

and may optionally return ``{"skipped": True, "skip_reason": str}``. The runner
(run.py) wraps timing, error capture and tier/credential gating around it — the
suite functions themselves stay simple.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Callable

# Grading modes, mirroring ClawBench Core v1.
AUTOMATED = "automated"      # precise deterministic comparison
LLM_JUDGE = "llm_judge"      # frontier model judges the outcome
HYBRID = "hybrid"            # deterministic structure + judged quality

# Tiers.
CORE = "core"                # deterministic, no LLM, no creds — daily-safe
LIVE = "live"                # real LLM on the local profile (HERMES_RUN_LLM_EVALS)


@dataclass(frozen=True)
class Suite:
    id: str
    category: str
    mode: str
    tier: str
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
        tier=CORE,
        weight=1.0,
        runner="evals.hermesbench.suites.responsiveness:run",
        summary="Pre-LLM ack accuracy, false-ack rate, progress cadence (deterministic).",
    ),
    Suite(
        id="kanban_scale",
        category="Kanban kernel scale",
        mode=AUTOMATED,
        tier=CORE,
        weight=0.7,
        runner="evals.hermesbench.suites.kanban_scale:run",
        summary="Dispatch / recompute / context / query latency at 100..10k tasks.",
    ),
    Suite(
        id="orchestrator",
        category="Orchestration & routing",
        mode=HYBRID,
        tier=LIVE,
        weight=1.0,
        runner="evals.hermesbench.suites.orchestrator:run",
        summary="Real orchestrator routing/linking accuracy over isolated boards.",
    ),
    Suite(
        id="origin_return",
        category="End-to-end task return",
        mode=LLM_JUDGE,
        tier=LIVE,
        weight=0.8,
        runner="evals.hermesbench.suites.origin_return:run",
        summary="Front-desk turn -> task -> completion returns to the originator.",
    ),
]


def all_suites() -> list[Suite]:
    return list(_SUITES)


def by_id(suite_id: str) -> Suite | None:
    return next((s for s in _SUITES if s.id == suite_id), None)


def select(*, tier: str | None = None, ids: list[str] | None = None) -> list[Suite]:
    """Select suites for a run.

    ``tier='core'`` -> only core suites. ``tier='live'`` -> only live suites.
    ``tier='full'`` or None -> all suites (live ones get gated at execution time
    by HERMES_RUN_LLM_EVALS). ``ids`` further restricts to named suites.
    """
    out = _SUITES
    if tier in (CORE, LIVE):
        out = [s for s in out if s.tier == tier]
    if ids:
        wanted = set(ids)
        out = [s for s in out if s.id in wanted]
    return list(out)
