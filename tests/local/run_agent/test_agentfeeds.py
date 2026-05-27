"""Local AgentFeeds system-prompt tests (extracted from tests/run_agent/test_run_agent.py).

Kept out of the upstream test file so merges don't conflict on local test
additions. Reuses the upstream ``agent`` fixture and ``_make_tool_defs``
helper via import.
"""
from __future__ import annotations

from unittest.mock import patch

import run_agent
from run_agent import AIAgent

# Reuse upstream fixtures/helpers without duplicating them.
from tests.run_agent.test_run_agent import _make_tool_defs, agent  # noqa: F401


def test_agentfeeds_system_manifest_is_inserted_before_skills_when_enabled():
    tools = _make_tool_defs("skills_list", "skill_view", "skill_manage")
    with (
        patch("run_agent.get_tool_definitions", return_value=tools),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value={"agentfeeds": {"system_prompt": {"enabled": True}}}),
        patch("run_agent._build_agentfeeds_system_manifest", return_value="<agentfeeds>\n- weather: santa-clara-current\n</agentfeeds>"),
        patch("run_agent.build_skills_system_prompt", return_value="SKILLS_PROMPT"),
    ):
        agent = AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        prompt = agent._build_system_prompt()

    assert "<agentfeeds>" in prompt
    assert prompt.index("<agentfeeds>") < prompt.index("SKILLS_PROMPT")


def test_agentfeeds_manifest_renderer_groups_and_caps_without_volatile_fields():
    manifest = run_agent._render_agentfeeds_manifest(
        ["weather/santa-clara-current", "finance/quote-msft", "finance/quote-btc", "finance/quote-spy"],
        max_per_group=2,
    )

    assert manifest is not None
    assert "- weather: santa-clara-current" in manifest
    assert "- finance: quote-msft, quote-btc, ... (+1 more)" in manifest
    assert "stale" not in manifest.lower()
    assert "updated" not in manifest.lower()
    assert "health" not in manifest.lower()


class TestAgentFeedsManifestPlacement:
    """The AgentFeeds system manifest is a stable, per-session inventory and
    must be appended to the cache-stable region of the system prompt
    (``stable_parts``), not the volatile/context tail that would defeat
    prompt caching when it shifts.

    Regression: the pre-patch code appended to a non-existent ``prompt_parts``
    local, which raised ``NameError`` whenever the manifest was enabled and
    non-empty. Building the prompt with a live manifest must not raise.
    """

    _SENTINEL = "AGENTFEEDS-MANIFEST-SENTINEL-7f3a"

    def test_manifest_lands_in_stable_region_only(self, agent, monkeypatch):
        monkeypatch.setattr(
            run_agent, "_build_agentfeeds_system_manifest",
            lambda *a, **k: self._SENTINEL,
        )
        agent._agentfeeds_system_prompt_config = {"enabled": True}

        parts = agent._build_system_prompt_parts()

        assert self._SENTINEL in parts["stable"]
        assert self._SENTINEL not in parts["context"]
        assert self._SENTINEL not in parts["volatile"]

    def test_disabled_manifest_omitted_without_error(self, agent, monkeypatch):
        monkeypatch.setattr(
            run_agent, "_build_agentfeeds_system_manifest",
            lambda *a, **k: None,
        )
        agent._agentfeeds_system_prompt_config = {}

        parts = agent._build_system_prompt_parts()

        assert self._SENTINEL not in parts["stable"]
        assert isinstance(parts["stable"], str)
