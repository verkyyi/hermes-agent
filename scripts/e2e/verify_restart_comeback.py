#!/usr/bin/env python3
"""E2E verification (no pytest): /restart always replies to the initiating channel.

Drives the REAL gateway machinery end-to-end through the actual restart-marker
handoff, without pytest and without touching the live launchd gateway or sending
real Telegram messages:

  Phase A  real TelegramAdapter -> real GatewayRunner._handle_message -> the real
           /restart command handler, which writes ~/.hermes/.restart_notify.json
           (the cross-process contract). request_restart is stubbed so we don't
           actually exec a reboot; everything else is the production code path.
  Phase B  a fresh runner (simulating the rebooted process) runs the real
           _send_restart_notification(), which reads the marker and sends the
           "gateway restarted" comeback to the initiating chat.
  Phase C  the real home-channel broadcast path stays SILENT for a channel that
           did NOT initiate the restart.

All three run with telegram.gateway_restart_notification=False — the user's real
config — proving the initiator hears back while the broadcast stays muted.

Run from the repo root (use -m so it tests the checkout you're standing in;
plain `python scripts/...` resolves `gateway` via the editable install and may
silently test a different checkout when run from a worktree):
    venv/bin/python -m scripts.e2e.verify_restart_comeback [-v]

Exit code 0 = PASS, 1 = FAIL. The adapter's send() is recorded (real adapter
object, no network), and HERMES_HOME is an isolated temp dir.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from tests.e2e.conftest import make_adapter, make_runner, send_and_capture

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
INITIATOR_CHAT = "e2e-chat-1"  # default chat_id used by tests.e2e.conftest.make_source


def _log(msg: str) -> None:
    if VERBOSE:
        print(f"  · {msg}")


async def _run() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    tmp_home = Path(tempfile.mkdtemp(prefix="hermes-e2e-restart-"))
    # Point the whole module at an isolated home so we never touch ~/.hermes.
    gateway_run._hermes_home = tmp_home
    _log(f"isolated HERMES_HOME = {tmp_home}")

    notify_path = tmp_home / ".restart_notify.json"

    # ── Phase A: user types /restart through the real adapter pipeline ──────
    runner = make_runner(Platform.TELEGRAM)
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    # Don't actually reboot — just record that a restart was requested.
    runner.request_restart = MagicMock(return_value=True)
    adapter = make_adapter(Platform.TELEGRAM, runner)

    await send_and_capture(adapter, "/restart", Platform.TELEGRAM)

    requested = runner.request_restart.called
    marker_written = notify_path.exists()
    marker_chat = None
    if marker_written:
        marker_chat = json.loads(notify_path.read_text()).get("chat_id")
    _log(f"request_restart called={requested}, marker_written={marker_written}, chat={marker_chat}")
    results.append((
        "A: /restart routes through real pipeline and persists the notify marker",
        requested and marker_written and marker_chat == INITIATOR_CHAT,
        f"requested={requested} marker={marker_written} chat={marker_chat!r}",
    ))

    # ── Phase B: rebooted process notifies the INITIATING channel ───────────
    fresh = make_runner(Platform.TELEGRAM)
    fresh.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    fresh_adapter = make_adapter(Platform.TELEGRAM, fresh)
    fresh_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="comeback"))

    delivered = await fresh._send_restart_notification()

    sent = fresh_adapter.send.await_count == 1
    to_initiator = sent and fresh_adapter.send.call_args[0][0] == INITIATOR_CHAT
    text = fresh_adapter.send.call_args[0][1] if sent else ""
    says_restarted = "restart" in text.lower()
    cleaned_up = not notify_path.exists()
    _log(f"delivered={delivered} sent={sent} to_initiator={to_initiator} text={text!r}")
    results.append((
        "B: rebooted gateway replies to the initiating channel despite flag=false",
        bool(delivered) and sent and to_initiator and says_restarted and cleaned_up,
        f"delivered={delivered} text={text!r} cleaned_up={cleaned_up}",
    ))

    # ── Phase C: non-initiating home-channel broadcast stays muted ──────────
    bystander = make_runner(Platform.TELEGRAM)
    bystander.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="home-bystander", name="Ops Home",
    )
    bystander.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    bystander_adapter = make_adapter(Platform.TELEGRAM, bystander)
    bystander_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="x"))

    broadcast = await bystander._send_home_channel_startup_notifications()
    silent = broadcast == set() and bystander_adapter.send.await_count == 0
    _log(f"broadcast_targets={broadcast} send_count={bystander_adapter.send.await_count}")
    results.append((
        "C: home-channel broadcast stays silent for a non-initiating channel",
        silent,
        f"broadcast={broadcast} sends={bystander_adapter.send.await_count}",
    ))

    return results


def main() -> int:
    results = asyncio.run(_run())
    print("\nE2E: /restart comeback to initiating channel (real harness, no pytest)\n")
    all_ok = True
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"  [{mark}] {name}")
        if not ok:
            print(f"         → {detail}")
    print()
    if all_ok:
        print("RESULT: PASS — initiator always notified; broadcast respects the flag.")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
