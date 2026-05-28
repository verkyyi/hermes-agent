#!/usr/bin/env python3
"""E2E verification (no pytest): context-aware LLM upgrade of the pre-LLM ack.

The gateway sends a deterministic ack ("Got it — checking.") inside the sub-300ms
TTFF budget, then — off the critical path — asks the LLM (via the provider-
agnostic auxiliary client, mirroring the main agent's provider/model) for a
one-line ack grounded in what the user said, and EDITS the bubble in place.
This exercises the REAL gateway seams (config resolution, the compose function's
prompt/parse logic, and the background upgrade method that calls the adapter's
real edit_message contract) without network, without the heavy agent run, and
without touching the live launchd gateway.

  Phase A  _upgrade_ack_with_llm happy path: a stubbed compose returns a line and
           the runner edits the bubble in place via adapter.edit_message.
  Phase B  feature disabled (HERMES_RESP_LLM_ACK=0) → no edit; deterministic ack
           stays.
  Phase C  compose returns None (provider unavailable) → no edit.
  Phase D  adapter without edit_message (e.g. Weixin) → no crash, no edit.
  Phase E  the REAL _compose_llm_ack: with a stubbed auxiliary client (no
           network) it assembles the request, parses/cleans the reply, uses the
           main provider's model by default, and honors an explicit override.

Run from the repo root (use -m so it tests the checkout you're standing in;
plain `python scripts/...` resolves `gateway` via the editable install and may
silently test a different checkout when run from a worktree):
    venv/bin/python -m scripts.e2e.verify_llm_ack_upgrade [-v]

Exit code 0 = PASS, 1 = FAIL.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import SendResult
from tests.e2e.conftest import make_adapter, make_runner

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
CHAT = "e2e-chat-1"
MSG_ID = "ack-msg-1"


def _log(msg: str) -> None:
    if VERBOSE:
        print(f"  · {msg}")


def _runner_with_editing_adapter():
    runner = make_runner(Platform.TELEGRAM)
    adapter = make_adapter(Platform.TELEGRAM, runner)
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id=MSG_ID))
    return runner, adapter


def _async_const(value):
    async def _fn(message_text, ack_cfg):
        return value
    return _fn


async def _run() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    # ── Phase A: happy path — compose returns a line, bubble edited in place ──
    runner, adapter = _runner_with_editing_adapter()
    line = "On it — checking the gateway logs now."
    orig_compose = gateway_run._compose_llm_ack
    gateway_run._compose_llm_ack = _async_const(line)
    try:
        os.environ.pop("HERMES_RESP_LLM_ACK", None)
        await runner._upgrade_ack_with_llm(
            adapter=adapter,
            chat_id=CHAT,
            message_id=MSG_ID,
            message_text="please check the gateway logs for the restart loop",
            thread_id=None,
            gateway_request_id="req-A",
        )
    finally:
        gateway_run._compose_llm_ack = orig_compose
    called = adapter.edit_message.await_count == 1
    args = adapter.edit_message.call_args
    ok_args = bool(called and args and args[0][0] == CHAT and args[0][1] == MSG_ID
                   and args[0][2] == line and args.kwargs.get("finalize") is True)
    _log(f"edit called={called} args={args}")
    results.append((
        "A: upgrade edits the ack bubble in place with the composed line",
        ok_args,
        f"called={called} args={args}",
    ))

    # ── Phase B: disabled via env → no edit ───────────────────────────────────
    runner, adapter = _runner_with_editing_adapter()
    orig_compose = gateway_run._compose_llm_ack
    gateway_run._compose_llm_ack = _async_const("should not be used")
    os.environ["HERMES_RESP_LLM_ACK"] = "0"
    try:
        await runner._upgrade_ack_with_llm(
            adapter=adapter, chat_id=CHAT, message_id=MSG_ID,
            message_text="check the gateway logs", thread_id=None, gateway_request_id="req-B",
        )
    finally:
        gateway_run._compose_llm_ack = orig_compose
        os.environ.pop("HERMES_RESP_LLM_ACK", None)
    results.append((
        "B: HERMES_RESP_LLM_ACK=0 disables the upgrade (no edit)",
        adapter.edit_message.await_count == 0,
        f"edits={adapter.edit_message.await_count}",
    ))

    # ── Phase C: compose returns None → no edit (deterministic ack stays) ─────
    runner, adapter = _runner_with_editing_adapter()
    orig_compose = gateway_run._compose_llm_ack
    gateway_run._compose_llm_ack = _async_const(None)
    try:
        await runner._upgrade_ack_with_llm(
            adapter=adapter, chat_id=CHAT, message_id=MSG_ID,
            message_text="check the gateway logs", thread_id=None, gateway_request_id="req-C",
        )
    finally:
        gateway_run._compose_llm_ack = orig_compose
    results.append((
        "C: compose -> None leaves the deterministic ack untouched (no edit)",
        adapter.edit_message.await_count == 0,
        f"edits={adapter.edit_message.await_count}",
    ))

    # ── Phase D: adapter without edit_message (Weixin) → no crash, no edit ────
    runner = make_runner(Platform.TELEGRAM)
    # Bare stub with no edit_message, mirroring the Weixin adapter which cannot
    # edit a sent message; the upgrade method must bail on the missing attr.
    no_edit_adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    crashed = False
    try:
        await runner._upgrade_ack_with_llm(
            adapter=no_edit_adapter, chat_id=CHAT, message_id=MSG_ID,
            message_text="check the gateway logs", thread_id=None, gateway_request_id="req-D",
        )
    except Exception as exc:  # pragma: no cover - failure path
        crashed = True
        _log(f"unexpected crash: {exc}")
    results.append((
        "D: adapter without edit_message is handled gracefully (no crash)",
        not crashed,
        f"crashed={crashed}",
    ))

    # ── Phase E: the real _compose_llm_ack logic (stubbed auxiliary client) ───
    import agent.auxiliary_client as aux

    captured = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='"Looking into the\n restart loop now."'))])

    class _FakeClient:
        chat = SimpleNamespace(completions=_FakeCompletions())

    orig_resolve = gateway_run._resolve_runtime_agent_kwargs
    orig_model = gateway_run._resolve_gateway_model
    orig_get = aux.get_async_text_auxiliary_client
    gateway_run._resolve_runtime_agent_kwargs = lambda: {"provider": "openai-codex", "api_mode": "codex_responses"}
    gateway_run._resolve_gateway_model = lambda config=None: "gpt-5.5"
    aux.get_async_text_auxiliary_client = lambda task="", *, main_runtime=None: (_FakeClient(), "gpt-5.5")
    try:
        # E1: default model → uses the main provider's resolved model.
        e1 = await gateway_run._compose_llm_ack("why is the gateway in a restart loop?", {"model": "", "timeout_s": 6.0})
        e1_clean = e1 == "Looking into the restart loop now."   # whitespace collapsed, quotes stripped
        e1_model = captured.get("model") == "gpt-5.5"
        e1_sys = any(m.get("role") == "system" and "one line" in m.get("content", "").lower()
                     for m in captured.get("messages", []))
        _log(f"E1 line={e1!r} model={captured.get('model')!r}")
        results.append((
            "E1: compose parses/cleans reply and uses the main provider's model",
            bool(e1_clean and e1_model and e1_sys),
            f"line={e1!r} model={captured.get('model')!r} sys_ok={e1_sys}",
        ))
        # E2: explicit model override is honored.
        e2 = await gateway_run._compose_llm_ack("check logs", {"model": "claude-haiku-4-5-20251001", "timeout_s": 6.0})
        results.append((
            "E2: explicit responsiveness.llm_ack.model override is honored",
            captured.get("model") == "claude-haiku-4-5-20251001" and bool(e2),
            f"model={captured.get('model')!r} line={e2!r}",
        ))
    finally:
        gateway_run._resolve_runtime_agent_kwargs = orig_resolve
        gateway_run._resolve_gateway_model = orig_model
        aux.get_async_text_auxiliary_client = orig_get

    # E3: auxiliary client unavailable → None (no edit downstream).
    orig_get = aux.get_async_text_auxiliary_client
    orig_resolve = gateway_run._resolve_runtime_agent_kwargs
    gateway_run._resolve_runtime_agent_kwargs = lambda: {"model": "x"}
    aux.get_async_text_auxiliary_client = lambda task="", *, main_runtime=None: (None, None)
    try:
        e3 = await gateway_run._compose_llm_ack("check logs", {"model": "", "timeout_s": 6.0})
    finally:
        aux.get_async_text_auxiliary_client = orig_get
        gateway_run._resolve_runtime_agent_kwargs = orig_resolve
    results.append((
        "E3: no auxiliary client available -> compose returns None",
        e3 is None,
        f"result={e3!r}",
    ))

    return results


def main() -> int:
    results = asyncio.run(_run())
    print("\nE2E: context-aware LLM ack upgrade (real harness, no pytest)\n")
    all_ok = True
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"  [{mark}] {name}")
        if not ok:
            print(f"         → {detail}")
    print()
    if all_ok:
        print("RESULT: PASS — deterministic ack stays instant; LLM upgrade edits in place when eligible.")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
