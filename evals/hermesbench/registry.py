"""Suite registry for HermesBench (v2 — black-box, default-profile, reliability-first).

Each suite is one use-case *category*. Its `run()` drives the default profile as
an end user (isolated turn), judges the reply, and returns a normalized
``{score: 0..100, passed: bool, metrics: {...}}`` (or ``{skipped: True,
skip_reason}``). Suites evaluate purely from the user's perspective — no
kanban/orchestrator internals — and weight reliability/responsiveness/closure
above capability (see suites/usecases.py and METHODOLOGY.md).

All suites drive real agents, so they self-skip when HERMES_RUN_LLM_EVALS is
unset. The architecture-coupled internal evals (kanban scale, orchestrator
routing, origin-return) were retired from the bench; they still exist standalone
under evals/ and tests/stress for ad-hoc internal checks.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

from evals.hermesbench import usecases

# Grading modes, mirroring ClawBench Core v1. Every v2 suite is ``hybrid``:
# mechanical reliability signals (responded / latency / stable / concluded) plus
# an LLM judge for conclusion-type, appropriateness, and coherence.
AUTOMATED = "automated"
LLM_JUDGE = "llm_judge"
HYBRID = "hybrid"


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


_RUNNER = "evals.hermesbench.suites.usecases:run_{}"

# One suite per use-case category. Equal weight — the reliability-over-capability
# bias lives inside each suite's score formula, not in the cross-category weights.
_SUITES: list[Suite] = [
    Suite(
        id=cat,
        category=usecases.CATEGORY_LABELS.get(cat, cat),
        mode=HYBRID,
        weight=1.0,
        runner=_RUNNER.format(cat),
        summary=f"Black-box {usecases.CATEGORY_LABELS.get(cat, cat)} use cases — "
                "closure, stability, responsiveness, appropriateness.",
    )
    for cat in usecases.categories()
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
