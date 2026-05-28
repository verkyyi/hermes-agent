"""Local coverage: orchestrator guidance prefers kanban_decompose (self-park).

Shallow presence assertions over the injected guidance + the orchestrator skill
text. They guard against the guidance silently reverting to the old
create-then-complete pattern, which would defeat the decompose-anchor design
(the orchestrator would complete its task with a "decomposed" non-answer instead
of parking as the fan-in anchor). See
docs/plans/2026-05-28-kanban-wake-origin-session.md.
"""

from __future__ import annotations

from pathlib import Path

from agent.prompt_builder import KANBAN_GUIDANCE


def _skill_md() -> str:
    # tests/local/agent/<this file> -> repo root is parents[3]
    root = Path(__file__).resolve().parents[3]
    return (root / "skills" / "devops" / "kanban-orchestrator" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_kanban_guidance_orchestrator_mode_prefers_decompose():
    text = KANBAN_GUIDANCE.lower()
    assert "kanban_decompose" in text
    # The self-park / fan-in anchor contract must be stated.
    assert "anchor" in text
    # The critical rule: do not complete in the same turn you decompose.
    assert "do not call `kanban_complete`" in text


def test_orchestrator_skill_documents_decompose_self_park():
    md = _skill_md().lower()
    assert "kanban_decompose" in md
    # Step 4 must tell the orchestrator NOT to complete immediately.
    assert "do not call `kanban_complete`" in md
    # parents-as-indices contract (distinct from kanban_create's task ids).
    assert "indices into the `children`" in md
