"""Fork-local: /restart always replies to the initiating channel.

Behavior change (verky/deploy, 2026-05-28) — diverges from upstream:
the `gateway_restart_notification` flag governs only the *unsolicited*
home-channel "Gateway online" broadcast. The chat that explicitly typed
`/restart` always gets a direct "gateway restarted" reply when the gateway
comes back, even with the flag set to `false`.

Symptom that motivated this: user set telegram.gateway_restart_notification=false
to mute the noisy home-channel broadcast and then never saw a comeback message
after /restart (confirmed in ~/.hermes/logs/gateway.log: "Restart notification
suppressed: telegram has gateway_restart_notification=false").

Lives under tests/local/ per the fork convention (never edit upstream test
files). The one unavoidable in-place edit to the upstream suite is
tests/gateway/test_restart_notification.py::test_send_restart_notification_ignores_flag_for_initiator,
which had asserted the old (now-reversed) suppression behavior.
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


@pytest.mark.asyncio
async def test_initiating_channel_gets_comeback_even_when_flag_disabled(tmp_path, monkeypatch):
    """The channel that ran /restart is notified on comeback even with the flag off."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    # --- old process: user types /restart from chat "42" ---
    runner, adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="x"))

    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="42"),
        message_id="m1",
    )
    await runner._handle_restart_command(event)
    assert (tmp_path / ".restart_notify.json").exists()

    # --- new process: gateway comes back up ---
    fresh_runner, fresh_adapter = make_restart_runner()
    fresh_runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    fresh_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="y"))

    delivered = await fresh_runner._send_restart_notification()

    assert delivered == ("telegram", "42", None)
    fresh_adapter.send.assert_called_once()
    assert fresh_adapter.send.call_args[0][0] == "42"
    assert "restarted" in fresh_adapter.send.call_args[0][1].lower()
    assert not (tmp_path / ".restart_notify.json").exists()


@pytest.mark.asyncio
async def test_non_initiating_home_channel_stays_silent_when_flag_disabled(tmp_path, monkeypatch):
    """A home channel that did NOT trigger /restart stays muted when the flag is off.

    Guards against over-correcting into spamming every home channel on restart.
    """
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-1",
        name="Ops Home",
    )
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="z"))

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == set()
    adapter.send.assert_not_called()


@pytest.mark.asyncio
async def test_initiating_channel_gets_comeback_when_flag_enabled(tmp_path, monkeypatch):
    """Control: the initiating channel is also notified when the flag is on."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    assert runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification is True
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="x"))

    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(chat_id="42"),
        message_id="m1",
    )
    await runner._handle_restart_command(event)

    fresh_runner, fresh_adapter = make_restart_runner()
    fresh_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="y"))

    delivered = await fresh_runner._send_restart_notification()

    assert delivered == ("telegram", "42", None)
    fresh_adapter.send.assert_called_once()
    assert "restarted" in fresh_adapter.send.call_args[0][1].lower()
