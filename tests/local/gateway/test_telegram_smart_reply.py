"""Local tests: reply_to_mode="smart" — context-aware quoting.

Fork-local addition kept under tests/local/ so upstream merges don't conflict.
"smart" is a new, additive reply_to_mode value: it suppresses the redundant
reply-to quote in a linear 1:1 DM, but still quotes the first chunk in groups
and on out-of-band DM replies (newer messages arrived while the agent worked),
where the quote disambiguates which message is being answered. The existing
off/first/all modes are unchanged.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
    _apply_env_overrides,
)

# Reuse the upstream module's telegram-import shim, then build adapters directly.
import tests.gateway.test_telegram_reply_mode  # noqa: F401  (installs telegram mock)
from gateway.platforms.telegram import TelegramAdapter


def _smart_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="t", reply_to_mode="smart"))


# ── _should_thread_reply under smart mode ────────────────────────────────


def test_no_reply_to_returns_false():
    adapter = _smart_adapter()
    assert adapter._should_thread_reply(None, 0, chat_id="123") is False


def test_linear_one_to_one_dm_suppresses_quote():
    adapter = _smart_adapter()
    adapter._last_inbound_message_id = {"123": "999"}
    assert adapter._should_thread_reply("999", 0, chat_id="123") is False
    assert adapter._should_thread_reply("999", 1, chat_id="123") is False


def test_unknown_last_inbound_treated_as_in_order():
    adapter = _smart_adapter()
    adapter._last_inbound_message_id = {}
    assert adapter._should_thread_reply("999", 0, chat_id="123") is False


def test_out_of_band_dm_reply_quotes_first_chunk():
    adapter = _smart_adapter()
    adapter._last_inbound_message_id = {"123": "1001"}  # newer than answered 999
    assert adapter._should_thread_reply("999", 0, chat_id="123") is True
    assert adapter._should_thread_reply("999", 1, chat_id="123") is False


def test_group_chat_quotes_first_chunk():
    adapter = _smart_adapter()
    adapter._last_inbound_message_id = {"-1001234": "555"}
    assert adapter._should_thread_reply("555", 0, chat_id="-1001234") is True
    assert adapter._should_thread_reply("555", 1, chat_id="-1001234") is False


def test_dm_topic_lane_quotes():
    adapter = _smart_adapter()
    adapter._last_inbound_message_id = {"123": "999"}
    assert adapter._should_thread_reply("999", 0, chat_id="123", thread_id="77") is True


def test_non_numeric_chat_id_treated_as_non_dm():
    adapter = _smart_adapter()
    adapter._last_inbound_message_id = {}
    assert adapter._should_thread_reply("999", 0, chat_id="@channelname") is True


# ── bounded inbound tracking ─────────────────────────────────────────────


def test_record_inbound_is_bounded_and_lru():
    adapter = _smart_adapter()
    adapter._last_inbound_max_chats = 3
    for i in range(5):
        adapter._record_inbound_message(f"chat{i}", str(i))
    assert len(adapter._last_inbound_message_id) == 3
    # Oldest two evicted; newest three retained.
    assert "chat0" not in adapter._last_inbound_message_id
    assert "chat4" in adapter._last_inbound_message_id


# ── send() end-to-end under smart mode ───────────────────────────────────


@pytest.mark.asyncio
async def test_send_smart_dm_no_threading():
    adapter = _smart_adapter()
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    adapter.truncate_message = lambda content, max_len, **kw: ["c1", "c2"]
    adapter._last_inbound_message_id = {"12345": "999"}

    await adapter.send("12345", "test content", reply_to="999")

    for call in adapter._bot.send_message.call_args_list:
        assert call.kwargs.get("reply_to_message_id") is None


@pytest.mark.asyncio
async def test_send_smart_group_threads_first_chunk():
    adapter = _smart_adapter()
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    adapter.truncate_message = lambda content, max_len, **kw: ["c1", "c2", "c3"]
    adapter._last_inbound_message_id = {}

    await adapter.send("-1009876", "test content", reply_to="555")

    calls = adapter._bot.send_message.call_args_list
    assert calls[0].kwargs.get("reply_to_message_id") == 555
    assert calls[1].kwargs.get("reply_to_message_id") is None
    assert calls[2].kwargs.get("reply_to_message_id") is None


# ── config plumbing: "smart" is accepted as a reply_to_mode ───────────────


def test_env_var_sets_smart_mode():
    config = GatewayConfig()
    config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True, token="test")
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {"TELEGRAM_REPLY_TO_MODE": "smart"}, clear=False):
        _apply_env_overrides(config)
    assert config.platforms[Platform.TELEGRAM].reply_to_mode == "smart"
