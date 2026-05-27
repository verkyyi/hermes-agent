"""The routing rubric — a pure function over the board the orchestrator built.

Fed by both the mock validator (tests/evals/test_orchestrator_routing.py) and
the real run (run.py). The orchestrator legitimately builds DAGs (pipelines:
research -> ops -> synth), so routing components score over the WHOLE descendant
graph, not just the root's direct children.

Fan-in is verified STRICTLY because it is the single node responsible for the
notification back to the originating human — scattered notifications are the
failure mode we are guarding against. Two axes:
  * structural (always enforced): exactly ONE sink, and every other work node
    converges into it (it transitively depends on all of them). One terminal
    delivery node, provably.
  * mode (enforced only when observable): the sink is `synthesize`, every other
    work node is `silent`. In a headless `chat -q` run the create tool never
    persists notification_mode (no chat origin), so modes read back as None —
    reported as "unobservable", never silently passed. The mode axis is fully
    exercised in the deterministic mock suite where subs are controllable.
"""

from __future__ import annotations

from typing import Optional

from hermes_cli import kanban_db as kb


def _mode_of(conn, task_id: str) -> Optional[str]:
    subs = kb.list_notify_subs(conn, task_id=task_id)
    return subs[0]["notification_mode"] if subs else None


def _descendants(conn, root_id: str) -> list:
    """All tasks reachable downward from root via parent->child links."""
    seen, out, stack = set(), [], list(kb.child_ids(conn, root_id))
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        out.append(kb.get_task(conn, cid))
        stack.extend(kb.child_ids(conn, cid))
    return out


def _ancestors(conn, node_id: str) -> set:
    """All tasks reachable upward via child->parent links (excludes self)."""
    seen, stack = set(), list(kb.parent_ids(conn, node_id))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(kb.parent_ids(conn, pid))
    return seen


def score_routing(conn, root_id: str, case: dict) -> dict:
    """Grade one orchestrator routing attempt. Returns {components, passed, critical}."""
    root = kb.get_task(conn, root_id)
    kind = case["kind"]
    desc = _descendants(conn, root_id)
    desc_ids = {t.id for t in desc}
    direct_children = [kb.get_task(conn, c) for c in kb.child_ids(conn, root_id)]
    c: dict = {}

    # --- clarify: an unroutable task must be BLOCKED for clarification, not routed.
    if kind == "clarify":
        c["blocked_for_clarification"] = root.status == "blocked"
        c["created_no_work"] = len(desc) == 0
        critical = ["blocked_for_clarification", "created_no_work"]
        c["passed"] = all(c.get(k, False) for k in critical)
        return {"components": c, "passed": c["passed"], "critical": critical}

    # --- shared structural components
    c["routed"] = len(desc) >= 1
    c["self_completed"] = root.status in {"done", "archived"}
    # direct children must link to root; deeper nodes link to their parents by
    # construction (they're only descendants because the chain reaches root).
    c["linked"] = bool(direct_children) and all(
        root_id in kb.parent_ids(conn, t.id) for t in direct_children
    )
    all_assignees = {t.assignee for t in desc}
    c["correct_assignee"] = case["expected_assignees"].issubset(all_assignees)

    modes = {t.id: _mode_of(conn, t.id) for t in desc}
    observable = any(m is not None for m in modes.values())

    if kind == "multi":
        sinks = [t for t in desc if not kb.child_ids(conn, t.id)]
        c["single_sink"] = len(sinks) == 1
        if len(sinks) == 1:
            sink = sinks[0]
            anc = _ancestors(conn, sink.id)
            # every other work node must converge into the sink
            c["all_converge"] = (desc_ids - {sink.id}).issubset(anc)
            if observable:
                c["fanin_synthesize"] = modes.get(sink.id) == "synthesize"
                c["leaves_silent"] = all(
                    modes.get(t.id) in {"silent", None}
                    for t in desc if t.id != sink.id
                )
            else:  # headless: mode not persisted — don't pass it off as ok
                c["fanin_synthesize"] = None
                c["leaves_silent"] = None
        else:
            c["all_converge"] = False
            c["fanin_synthesize"] = False
            c["leaves_silent"] = False
        c["fanin_ok"] = bool(c["single_sink"] and c.get("all_converge"))
        critical = ["routed", "correct_assignee", "linked", "self_completed", "fanin_ok"]
    else:  # single
        c["single_subtask"] = len(direct_children) == 1
        leaf = direct_children[0] if direct_children else None
        if observable and leaf is not None:
            c["synthesize"] = modes.get(leaf.id) == "synthesize"
        else:
            c["synthesize"] = None
        critical = ["routed", "correct_assignee", "linked", "self_completed", "single_subtask"]

    passed = all(c.get(k, False) for k in critical)
    # Mode strictness blocks the pass only when observable AND wrong (False).
    # None (unobservable, headless) never blocks; structural checks carry the eval.
    for mode_key in ("fanin_synthesize", "leaves_silent", "synthesize"):
        if c.get(mode_key) is False:
            passed = False
    c["passed"] = passed
    return {"components": c, "passed": passed, "critical": critical}
