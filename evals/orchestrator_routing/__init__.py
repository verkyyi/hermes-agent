"""Orchestrator routing eval — Suite B of the orchestrator benchmark.

The orchestrator's routing judgment is the keystone of the whole 3-layer
design: if it does not (a) pick the right worker and (b) link every sub-task
back to the root, there is no ownership tree, and linking / group-by-ownership
notification / auto-unblock all rest on nothing.

This is an LLM-in-the-loop eval, not a deterministic unit test:
  * `dataset.py`  — routing cases mirroring profiles/orchestrator/SOUL.md.
  * `score.py`    — the rubric: a pure function over the resulting board state.
                    Same scorer feeds both the mock validator and the real run.
  * `run.py`      — invokes the REAL orchestrator (`hermes -p orchestrator ...`)
                    per case x N trials, scores, reports a pass-rate. Gated
                    behind HERMES_RUN_LLM_EVALS=1 (costs API calls).
"""
