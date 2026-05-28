"""Local tests: configurable per-outcome Telegram reactions.

Fork-local addition kept under tests/local/ so upstream merges don't conflict.
Reuses the upstream reaction-test helpers (`_make_adapter`, `_make_event`)
rather than duplicating them.

Default behavior is unchanged from upstream (👀 → 👍 / 👎); these tests cover
the new per-outcome env overrides, including the empty-value "clear instead of
set" semantics that lets a deployment stop stamping a repetitive 👍 on every
completed turn.
"""
from __future__ import annotations

import pytest

from gateway.platforms.base import ProcessingOutcome
from tests.gateway.test_telegram_reactions import _make_adapter, _make_event


# ── _reaction_emoji resolution ───────────────────────────────────────────


def test_reaction_emoji_defaults_match_upstream(monkeypatch):
    for env in ("TELEGRAM_REACTION_PROGRESS", "TELEGRAM_REACTION_SUCCESS", "TELEGRAM_REACTION_FAILURE"):
        monkeypatch.delenv(env, raising=False)
    adapter = _make_adapter()
    assert adapter._reaction_emoji("progress") == "\U0001f440"  # 👀
    assert adapter._reaction_emoji("success") == "\U0001f44d"   # 👍
    assert adapter._reaction_emoji("failure") == "\U0001f44e"   # 👎


def test_reaction_emoji_env_override_stripped(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "  \U0001f389  ")  # 🎉 padded
    adapter = _make_adapter()
    assert adapter._reaction_emoji("success") == "\U0001f389"


def test_reaction_emoji_empty_means_clear(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "")
    adapter = _make_adapter()
    assert adapter._reaction_emoji("success") == ""


# ── on_processing_complete: success ──────────────────────────────────────


@pytest.mark.asyncio
async def test_success_empty_clears_instead_of_setting(monkeypatch):
    """reaction_success="" clears the 👀 on success rather than leaving a 👍."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction=None,
    )


@pytest.mark.asyncio
async def test_success_emoji_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_SUCCESS", "\U0001f389")  # 🎉
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction="\U0001f389",
    )


# ── on_processing_complete: failure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_emoji_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_FAILURE", "\U0001f4a9")  # 💩
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction="\U0001f4a9",
    )


@pytest.mark.asyncio
async def test_failure_empty_clears(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_FAILURE", "")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction=None,
    )


# ── on_processing_start: progress ────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_emoji_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_PROGRESS", "\U0001f9d0")  # 🧐
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123, message_id=456, reaction="\U0001f9d0",
    )


@pytest.mark.asyncio
async def test_progress_empty_sets_no_reaction(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    monkeypatch.setenv("TELEGRAM_REACTION_PROGRESS", "")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()
