"""Live TTFT smoke test — gated, opt-in.

Doubly gated so it never runs in normal CI:
  * `integration` marker -> excluded by the repo's default `-m 'not integration'`.
  * skipif unless HERMES_RUN_LLM_EVALS=1 -> needs real provider credentials.

Run it explicitly:
    HERMES_RUN_LLM_EVALS=1 venv/bin/python -m pytest \
        tests/responsiveness_benchmark/test_live_ttft.py -m integration -q

It invokes the real default-profile agent on the daily requests with tools
disabled (text-only, no side effects) and asserts the model's measured
first-token latency is recorded and sane.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HERMES_RUN_LLM_EVALS") != "1",
        reason="live LLM eval: set HERMES_RUN_LLM_EVALS=1 (needs credentials) to run",
    ),
]

from evals.responsiveness.dataset import all_turns
from evals.responsiveness.run_live import DAILY_KINDS, aggregate, measure


def test_daily_requests_record_sane_ttft():
    daily = [t for t in all_turns() if t["kind"] in DAILY_KINDS]
    # Pin text-only so the tool_count==0 invariant below is deterministic.
    results = measure(daily, trials=1, tools_mode="none")

    measured = [r for r in results if r["ttft_ms"] is not None]
    assert measured, (
        "no TTFT recorded for any daily request — check provider credentials "
        f"and telemetry. first failure: {results[0] if results else 'none'}"
    )
    # First-token latency must be positive and sub-minute for a trivial prompt.
    for r in measured:
        assert 0 < r["ttft_ms"] < 60_000, r
        # Tools are disabled, so a daily turn must not have executed any tool.
        assert (r["tool_count"] or 0) == 0, r

    summary = aggregate(results)
    assert summary["daily_ttft_p50_ms"] is not None
