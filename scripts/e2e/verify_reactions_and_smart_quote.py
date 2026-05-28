#!/usr/bin/env python3
"""E2E verification (no pytest): semantic reactions + context-aware "smart" quoting.

Drives the REAL gateway message pipeline end-to-end (real TelegramAdapter, real
GatewayRunner._handle_message, real _process_message_background lifecycle, real
TelegramAdapter.send) without pytest, without touching the live launchd gateway,
and without any network. The agent call and the Telegram Bot client are mocked at
their edges; everything between is production code.

  Phase A  A normal text DM runs through the real pipeline with reactions on and
           reaction_success="" (the live-deploy config). The lifecycle stamps 👀
           on start and then CLEARS it on success, so a completed turn no longer
           leaves a repetitive 👍 on every message. The same run proves the agent
           reply is delivered WITHOUT a redundant reply-to quote in a linear 1:1
           DM under reply_to_mode="smart".
  Phase B  TELEGRAM_REACTION_SUCCESS=👍 restores the sticky success reaction
           through the same real pipeline.
  Phase C  In a group chat, smart mode DOES quote the first chunk (the quote
           disambiguates which message is being answered).

Run from the repo root (use -m so it tests the checkout you're standing in;
plain `python scripts/...` resolves `gateway` via the editable install and may
silently test a different checkout when run from a worktree):
    venv/bin/python -m scripts.e2e.verify_reactions_and_smart_quote [-v]

Exit code 0 = PASS, 1 = FAIL.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.telegram import TelegramAdapter
from tests.e2e.conftest import make_adapter, make_runner, make_source

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def _log(msg: str) -> None:
    if VERBOSE:
        print(f"  · {msg}")


def _numeric_event(chat_id: str, message_id: str, text: str, chat_type: str) -> MessageEvent:
    """A MessageEvent with numeric ids (Telegram reactions require int ids)."""
    return MessageEvent(
        text=text,
        source=make_source(Platform.TELEGRAM, chat_id=chat_id, chat_type=chat_type),
        message_id=message_id,
    )


def _wire_real_send(adapter: TelegramAdapter) -> MagicMock:
    """Restore the REAL send() on an adapter and mock only the Bot client.

    make_adapter() replaces send() with an AsyncMock; we want the production
    send()/_should_thread_reply path, so rebind the real method and record the
    Bot API calls instead.
    """
    adapter.send = TelegramAdapter.send.__get__(adapter, TelegramAdapter)
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    adapter._bot.set_message_reaction = AsyncMock()
    return adapter._bot


async def _drive(adapter, event) -> None:
    """Run one message through the real pipeline and let it settle."""
    await adapter.handle_message(event)
    await asyncio.sleep(0.4)


def _reaction_sequence(bot) -> list:
    """Ordered list of the `reaction` kwarg from each set_message_reaction call."""
    return [c.kwargs.get("reaction") for c in bot.set_message_reaction.call_args_list]


def _threaded_first_chunk(bot) -> object:
    """reply_to_message_id used on the first send_message call (None if none)."""
    calls = bot.send_message.call_args_list
    return calls[0].kwargs.get("reply_to_message_id") if calls else "no-send"


async def _run() -> list:
    results = []
    gateway_run._hermes_home = Path(tempfile.mkdtemp(prefix="hermes-e2e-react-"))
    os.environ["TELEGRAM_REACTIONS"] = "true"

    # ── Phase A: 1:1 DM — 👀 then cleared on success, and NO quote ──────────
    # The live deploy sets reaction_success="" (clear on success); simulate it.
    os.environ["TELEGRAM_REACTION_SUCCESS"] = ""
    runner = make_runner(Platform.TELEGRAM)
    runner.config.platforms[Platform.TELEGRAM].reply_to_mode = "smart"
    adapter = make_adapter(Platform.TELEGRAM, runner)
    adapter._reply_to_mode = "smart"
    bot = _wire_real_send(adapter)

    await _drive(adapter, _numeric_event("700123", "55", "what's the weather?", "dm"))

    seq = _reaction_sequence(bot)
    thread_first = _threaded_first_chunk(bot)
    _log(f"reaction sequence={seq!r}")
    _log(f"reply sent={bot.send_message.await_count}, first-chunk reply_to_id={thread_first!r}")
    react_ok = len(seq) == 2 and seq[0] == "\U0001f440" and seq[1] is None
    quote_ok = bot.send_message.await_count >= 1 and thread_first is None
    results.append((
        "A: 1:1 DM stamps 👀 then CLEARS on success (no repetitive 👍)",
        react_ok,
        f"reaction sequence={seq!r}",
    ))
    results.append((
        "A: 1:1 DM reply is delivered WITHOUT a redundant reply-to quote (smart)",
        quote_ok,
        f"sends={bot.send_message.await_count} first_chunk_reply_to={thread_first!r}",
    ))

    # ── Phase B: TELEGRAM_REACTION_SUCCESS restores a sticky success emoji ──
    os.environ["TELEGRAM_REACTION_SUCCESS"] = "\U0001f44d"  # 👍
    runner_b = make_runner(Platform.TELEGRAM)
    runner_b.config.platforms[Platform.TELEGRAM].reply_to_mode = "smart"
    adapter_b = make_adapter(Platform.TELEGRAM, runner_b)
    adapter_b._reply_to_mode = "smart"
    bot_b = _wire_real_send(adapter_b)

    await _drive(adapter_b, _numeric_event("700124", "56", "ping", "dm"))

    seq_b = _reaction_sequence(bot_b)
    _log(f"reaction sequence (success=👍)={seq_b!r}")
    results.append((
        "B: TELEGRAM_REACTION_SUCCESS restores 👍 through the real pipeline",
        len(seq_b) == 2 and seq_b[0] == "\U0001f440" and seq_b[1] == "\U0001f44d",
        f"reaction sequence={seq_b!r}",
    ))
    os.environ.pop("TELEGRAM_REACTION_SUCCESS", None)

    # ── Phase C: group chat — smart mode DOES quote the first chunk ─────────
    runner_c = make_runner(Platform.TELEGRAM)
    runner_c.config.platforms[Platform.TELEGRAM].reply_to_mode = "smart"
    adapter_c = make_adapter(Platform.TELEGRAM, runner_c)
    adapter_c._reply_to_mode = "smart"
    bot_c = _wire_real_send(adapter_c)

    await _drive(adapter_c, _numeric_event("-1009876", "80", "hey bot", "group"))

    thread_first_c = _threaded_first_chunk(bot_c)
    _log(f"group first-chunk reply_to_id={thread_first_c!r}")
    results.append((
        "C: group chat quotes the first chunk under smart mode",
        bot_c.send_message.await_count >= 1 and thread_first_c == 80,
        f"sends={bot_c.send_message.await_count} first_chunk_reply_to={thread_first_c!r}",
    ))

    return results


def main() -> int:
    results = asyncio.run(_run())
    print("\nE2E: semantic reactions + smart quoting (real harness, no pytest)\n")
    all_ok = True
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"  [{mark}] {name}")
        if not ok:
            print(f"         → {detail}")
    print()
    if all_ok:
        print("RESULT: PASS — reactions clear on success (configurable) and quoting is context-aware.")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
