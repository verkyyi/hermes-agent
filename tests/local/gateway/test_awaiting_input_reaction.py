"""Local tests: needs-input 🤔 reaction when the bot pauses for approval.

Fork-local feature. When the agent hits a dangerous-tool approval the turn
*blocks* until the user runs /approve or /deny, so the pending state is not
observable at on_processing_complete — the reliable signal is mid-turn, at the
approval-notify callback. There we stamp a configurable needs_input reaction
(default 🤔) on the triggering message; the existing completion hook overwrites
it with the final outcome reaction when the turn ends.

Two units:
  * TelegramAdapter.on_awaiting_input(chat_id, message_id) — sets the reaction
  * GatewayRunner._react_awaiting_input(source, message_id) — routes to the
    platform adapter if it supports the hook (best-effort, platform-agnostic)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_telegram_reactions import _make_adapter, _make_event
from tests.e2e.conftest import make_runner


# ── TelegramAdapter.on_awaiting_input ────────────────────────────────────


@pytest.mark.asyncio
async def test_awaiting_input_default_thinking_emoji(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.delenv("TELEGRAM_REACTION_NEEDS_INPUT", raising=False)
    adapter = _make_adapter()

    await adapter.on_awaiting_input("123", "456")

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction="\U0001f914",  # 🤔
    )


@pytest.mark.asyncio
async def test_awaiting_input_emoji_configurable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_NEEDS_INPUT", "\U0001f64f")  # 🙏
    adapter = _make_adapter()

    await adapter.on_awaiting_input("123", "456")

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction="\U0001f64f",
    )


@pytest.mark.asyncio
async def test_awaiting_input_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()

    await adapter.on_awaiting_input("123", "456")

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_awaiting_input_empty_emoji_sets_nothing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_NEEDS_INPUT", "")
    adapter = _make_adapter()

    await adapter.on_awaiting_input("123", "456")

    adapter._bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_awaiting_input_missing_ids_no_call(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()

    await adapter.on_awaiting_input(None, None)

    adapter._bot.set_message_reaction.assert_not_awaited()


# ── GatewayRunner._react_awaiting_input routing ──────────────────────────


def _source(platform=Platform.TELEGRAM, chat_id="700123"):
    return SessionSource(platform=platform, chat_id=chat_id, user_id="u1", user_name="t")


@pytest.mark.asyncio
async def test_runner_routes_to_adapter_hook():
    runner = make_runner(Platform.TELEGRAM)
    adapter = MagicMock()
    adapter.on_awaiting_input = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    await runner._react_awaiting_input(_source(), "55")

    adapter.on_awaiting_input.assert_awaited_once_with("700123", "55")


@pytest.mark.asyncio
async def test_runner_no_message_id_no_call():
    runner = make_runner(Platform.TELEGRAM)
    adapter = MagicMock()
    adapter.on_awaiting_input = AsyncMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    await runner._react_awaiting_input(_source(), None)

    adapter.on_awaiting_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_adapter_without_hook_is_noop():
    """A platform adapter that doesn't implement the hook must not raise."""
    runner = make_runner(Platform.TELEGRAM)
    # spec=[] → accessing .on_awaiting_input raises AttributeError if probed wrong
    adapter = MagicMock(spec=[])
    runner.adapters[Platform.TELEGRAM] = adapter

    # Should simply do nothing, not raise.
    await runner._react_awaiting_input(_source(), "55")


@pytest.mark.asyncio
async def test_runner_missing_adapter_is_noop():
    runner = make_runner(Platform.TELEGRAM)
    runner.adapters.clear()
    await runner._react_awaiting_input(_source(), "55")  # no raise


@pytest.mark.asyncio
async def test_runner_hook_error_is_swallowed():
    """Best-effort: a failing adapter hook must not propagate into the turn."""
    runner = make_runner(Platform.TELEGRAM)
    adapter = MagicMock()
    adapter.on_awaiting_input = AsyncMock(side_effect=RuntimeError("boom"))
    runner.adapters[Platform.TELEGRAM] = adapter

    await runner._react_awaiting_input(_source(), "55")  # no raise
