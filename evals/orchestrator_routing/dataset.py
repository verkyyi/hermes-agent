"""Routing cases — mirrors the worker table in profiles/orchestrator/SOUL.md.

Each case is a (body -> expected routing) expectation. `kind`:
  "single"  — one worker should handle it; orchestrator creates ONE sub-task.
  "multi"   — needs >1 stream (cross-type or gather->format pipeline). The
              orchestrator builds a DAG that converges into a SINGLE sink (the
              fan-in), which is the one node that notifies the human.
  "clarify" — genuinely unroutable; the orchestrator must BLOCK for
              clarification rather than route garbage to a fallback worker.

`expected_assignees` is the set that must appear somewhere in the DAG.

`expected_assignees` is the set the orchestrator's sub-tasks must cover. For
multi cases it's the set of leaf workers (the fan-in worker is not pinned —
worker-fast or worker-report are both acceptable per SOUL).
"""

from __future__ import annotations

CASES = [
    {
        "name": "web_research",
        "kind": "single",
        "expected_assignees": {"worker-research"},
        "body": (
            "Research the current state of the art in open-weight LLM "
            "inference engines (vLLM, TGI, SGLang). Summarize the tradeoffs "
            "and which is fastest for batched serving as of this month."
        ),
    },
    {
        "name": "ops_file_config",
        "kind": "single",
        "expected_assignees": {"worker-ops"},
        "body": (
            "Read ~/.hermes/config.yaml and report the current value of "
            "kanban.dispatch_interval_seconds and kanban.failure_limit. "
            "Do not change anything — read-only."
        ),
    },
    {
        "name": "code_pr",
        "kind": "single",
        "expected_assignees": {"worker-code"},
        "body": (
            "In the hermes-agent repo, add a --dry-run flag to the "
            "`hermes kanban archive` CLI command and open a PR with the change "
            "and a test."
        ),
    },
    {
        "name": "light_synthesis",
        "kind": "single",
        "expected_assignees": {"worker-fast"},
        "body": (
            "Rewrite this sentence to be more concise and professional: "
            "'we was thinking that maybe it could be a good idea to perhaps "
            "consider looking into the thing'."
        ),
    },
    {
        # A briefing naturally decomposes into gather (data) -> format (report).
        # worker-report is the delivering sink; the eval only requires it to
        # appear in the DAG that converges to a single fan-in.
        "name": "report_briefing",
        "kind": "multi",
        "expected_assignees": {"worker-report"},
        "body": (
            "Produce a one-page morning briefing for Verky: weather, markets "
            "(SPY/QQQ/BTC), and any urgent inbox signals, formatted for mobile."
        ),
    },
    {
        "name": "multi_research_plus_ops",
        "kind": "multi",
        "expected_assignees": {"worker-research", "worker-ops"},
        "body": (
            "Research the top 3 managed vector databases and their pricing, "
            "AND write the comparison into a new file ~/notes/vector-dbs.md. "
            "Give me one consolidated summary at the end."
        ),
    },
    {
        # Genuinely unroutable — correct behavior is to BLOCK for clarification,
        # not route to a fallback worker who also can't act.
        "name": "ambiguous_clarify",
        "kind": "clarify",
        "expected_assignees": set(),
        "body": "handle the thing we talked about",
    },
]
