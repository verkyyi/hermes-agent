"""Local tests for the Kanban completion-notification delivery path.

Kept in the tests/local/ tree so upstream merges don't conflict on local test
additions. Covers the redesigned delivery: config-resolved mode
(``_resolve_kanban_notify_mode``), the origin-session wake
(``_wake_origin_session`` / ``_wake_with_fallback``), and the direct/silent
paths. The old gateway-side LLM synthesis apparatus was removed (see
docs/plans/2026-05-28-kanban-wake-origin-session.md); its tests are gone with it.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import (
    GatewayRunner,
    _KANBAN_NOTIFY_KINDS,
    _kanban_heartbeat_progress_message,
    _public_progress_interval_from_env,
)
from gateway.session import SessionSource


class _Adapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append((chat_id, content, metadata))
        return self.result


async def _drain_wakes(runner):
    """Await the background origin-session wake task(s) the notifier scheduled.

    Synthesize-mode completion delivery is now non-blocking: the notifier
    schedules the wake on ``runner._background_tasks`` and returns immediately
    (see ``_run_kanban_wake_delivery``). Tests that assert on the delivered
    reply / cursor / unsubscribe must first drain those background tasks.
    """
    tasks = list(getattr(runner, "_background_tasks", set()) or [])
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Notifier watch set + progress helpers (unchanged behavior)
# ---------------------------------------------------------------------------

def test_kanban_notifier_watches_heartbeat_events_for_live_progress():
    assert "heartbeat" in _KANBAN_NOTIFY_KINDS
    for terminal_kind in ("completed", "blocked", "gave_up", "crashed", "timed_out"):
        assert terminal_kind in _KANBAN_NOTIFY_KINDS


def test_kanban_heartbeat_progress_message_is_public_and_phase_aware(monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/Users/verkyyi/.hermes")
    event = SimpleNamespace(
        kind="heartbeat",
        payload={"note": "terminal command running: sleep 130 && date (120s elapsed)"},
    )

    assert _kanban_heartbeat_progress_message(Platform.TELEGRAM, event) == (
        "Still working — running commands now."
    )
    assert _kanban_heartbeat_progress_message(Platform.DISCORD, event) is None


def test_public_progress_interval_env_clamps_and_disables(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "30")
    assert _public_progress_interval_from_env() == 120

    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0")
    assert _public_progress_interval_from_env() is None


# ---------------------------------------------------------------------------
# SendResult failure handling (direct path; no event -> never wakes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kanban_notification_sendresult_failure_raises(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=False, error="Not connected"))
    sub = {"task_id": "t_fail", "platform": "telegram", "chat_id": "123"}
    mirror_calls = []
    monkeypatch.setattr(
        "gateway.mirror.mirror_to_session",
        lambda *args, **kwargs: mirror_calls.append((args, kwargs)) or True,
    )

    with pytest.raises(RuntimeError, match="Not connected"):
        await runner._send_kanban_notification(adapter, sub, "done", {})

    assert adapter.calls == [("123", "done", {})]
    assert mirror_calls == []


@pytest.mark.asyncio
async def test_kanban_notification_sendresult_success_does_not_raise():
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {"task_id": "t_ok", "platform": "telegram", "chat_id": "123"}

    await runner._send_kanban_notification(adapter, sub, "done", {"thread_id": "7"})

    assert adapter.calls == [("123", "done", {"thread_id": "7"})]


@pytest.mark.asyncio
async def test_kanban_notification_emits_gateway_and_adapter_spans(monkeypatch):
    # Force direct mode so this exercises the adapter.send span path (telegram
    # would otherwise resolve to synthesize -> the wake path).
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "direct")
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {"task_id": "t_metrics", "platform": "telegram", "chat_id": "123"}
    task = SimpleNamespace(assignee="worker", status="done")
    event = SimpleNamespace(kind="completed")
    spans = []

    monkeypatch.setattr(
        "agent.telemetry.record_span_event",
        lambda name, **kwargs: spans.append((name, kwargs)),
    )
    monkeypatch.setattr("gateway.mirror.mirror_to_session", lambda *a, **k: True)

    await runner._send_kanban_notification(
        adapter, sub, "done text", {}, event=event, task=task
    )

    names = [name for name, _ in spans]
    assert "adapter.send.telegram" in names
    assert "gateway.send" in names
    for _name, kwargs in spans:
        attrs = kwargs.get("attributes") or {}
        assert attrs["platform"] == "telegram"
        assert attrs["notification_mode"] == "direct"
        assert attrs["task_status"] == "done"
        assert attrs["task_id"] == "t_metrics"
        assert "chat_id" not in attrs


# ---------------------------------------------------------------------------
# Mode resolution (operator policy via config, not the per-task sub column)
# ---------------------------------------------------------------------------

def test_resolve_notify_mode_env_overrides_all(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "direct")
    cfg = {"kanban": {"notify": {"telegram": {"mode": "synthesize"}}}}
    assert GatewayRunner._resolve_kanban_notify_mode("telegram", cfg) == "direct"


def test_resolve_notify_mode_per_platform_then_global(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_MODE", raising=False)
    cfg = {"kanban": {"notify": {"mode": "direct", "telegram": {"mode": "synthesize"}}}}
    assert GatewayRunner._resolve_kanban_notify_mode("telegram", cfg) == "synthesize"
    assert GatewayRunner._resolve_kanban_notify_mode("discord", cfg) == "direct"


def test_resolve_notify_mode_default_preserves_prior_behavior(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_MODE", raising=False)
    # No kanban.notify config -> built-in default mirrors the prior per-task
    # default (interactive Telegram synthesizes; everything else is direct).
    assert GatewayRunner._resolve_kanban_notify_mode("telegram", {}) == "synthesize"
    assert GatewayRunner._resolve_kanban_notify_mode("discord", {}) == "direct"
    assert GatewayRunner._resolve_kanban_notify_mode("weixin", {}) == "direct"


def test_resolve_notify_mode_ignores_invalid_values(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_MODE", raising=False)
    cfg = {"kanban": {"notify": {"mode": "bogus"}}}
    assert GatewayRunner._resolve_kanban_notify_mode("telegram", cfg) == "synthesize"


def test_kanban_wake_timeout_default_and_clamp():
    assert GatewayRunner._kanban_wake_timeout({}) == 180
    # clamped up to the 10s floor
    assert GatewayRunner._kanban_wake_timeout(
        {"kanban": {"notify": {"wake_timeout_seconds": 5}}}
    ) == 10
    # clamped down to the 600s ceiling
    assert GatewayRunner._kanban_wake_timeout(
        {"kanban": {"notify": {"wake_timeout_seconds": "900"}}}
    ) == 600
    # invalid -> default
    assert GatewayRunner._kanban_wake_timeout(
        {"kanban": {"notify": {"wake_timeout_seconds": "bad"}}}
    ) == 180


# ---------------------------------------------------------------------------
# Synthesize mode -> origin-session wake (completed only)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesize_completed_wakes_origin_session(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    sub = {"task_id": "t_syn", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "worker handoff"}, run_id=7)

    woke = {}

    async def fake_handle_message(event):
        woke["event"] = event
        return "synthesized reply"

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)

    await runner._send_kanban_notification(
        adapter, sub, "direct fallback", {}, event=ev, task=None, board="default"
    )
    # Wake is dispatched in the background (non-blocking); drain it before
    # asserting on the delivered reply.
    await _drain_wakes(runner)

    # The reply produced by the woken agent turn is delivered to the origin chat.
    assert adapter.calls and adapter.calls[0][0] == "123"
    assert adapter.calls[0][1] == "synthesized reply"
    # The trigger is a synthetic INTERNAL turn carrying the worker handoff.
    assert woke["event"].internal is True
    assert "worker handoff" in woke["event"].text


@pytest.mark.asyncio
async def test_synthesize_non_completed_event_sends_direct(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {"task_id": "t_blocked", "platform": "telegram", "chat_id": "123"}
    called = {"wake": False}

    async def fake_handle_message(event):
        called["wake"] = True
        return "x"

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)
    monkeypatch.setattr("gateway.mirror.mirror_to_session", lambda *a, **k: True)

    # A non-completed terminal event (e.g. blocked) is delivered directly, not
    # via a wake — only `completed` carries a deliverable answer.
    await runner._send_kanban_notification(
        adapter, sub, "blocked status line", {}, event=SimpleNamespace(kind="blocked"),
        task=None,
    )

    assert called["wake"] is False
    assert adapter.calls == [("123", "blocked status line", {})]


@pytest.mark.asyncio
async def test_wake_failure_falls_back_to_direct(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    runner.adapters = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {"task_id": "t_syn_fail", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "worker handoff"}, run_id=7)

    async def boom(event):
        raise RuntimeError("wake unavailable")

    monkeypatch.setattr(runner, "_handle_message", boom)

    # Wake errors -> the user still gets the direct status line. Delivery is
    # dispatched in the background, so drain before asserting.
    await runner._send_kanban_notification(
        adapter, sub, "direct fallback", {"thread_id": "9"}, event=ev, task=None
    )
    await _drain_wakes(runner)

    assert adapter.calls == [("123", "direct fallback", {"thread_id": "9"})]


@pytest.mark.asyncio
async def test_wake_with_fallback_timeout_falls_back_to_direct(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {"task_id": "t_to", "platform": "telegram", "chat_id": "123"}
    monkeypatch.setattr(
        GatewayRunner, "_kanban_wake_timeout", staticmethod(lambda *a, **k: 0.01)
    )

    async def slow(event):
        await asyncio.sleep(0.2)
        return "too late"

    monkeypatch.setattr(runner, "_handle_message", slow)

    await runner._wake_with_fallback(
        sub=sub, event=SimpleNamespace(kind="completed", payload={}),
        task=None, board=None, msg="direct line", metadata={}, adapter=adapter,
    )

    assert adapter.calls == [("123", "direct line", {})]


@pytest.mark.asyncio
async def test_silent_mode_noops(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "silent")
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {"task_id": "t_silent", "platform": "telegram", "chat_id": "123"}

    await runner._send_kanban_notification(adapter, sub, "direct fallback", {})

    assert adapter.calls == []


@pytest.mark.asyncio
async def test_synthesize_wake_runs_outside_conversation_lock(monkeypatch):
    """Regression: the wake must NOT run while the notifier holds the per-session
    conversation lock. ``_handle_message`` acquires that same lock, so holding it
    here self-deadlocks the wake (it hangs until the wake timeout, then degrades
    to a direct send). The fake turn asserts no conversation lock is held."""
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    sub = {"task_id": "t_lock", "platform": "telegram", "chat_id": "123", "user_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "h"}, run_id=1)

    seen = {}

    async def fake_handle_message(event):
        # Mirror the real agent turn: it will acquire the per-session lock. If
        # the notifier is (wrongly) holding it, one is already locked here.
        seen["any_locked"] = any(
            lock.locked() for lock in (runner._conversation_locks or {}).values()
        )
        return "reply"

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)

    # Bound the call so a regression (deadlock) surfaces as a failure, not a hang.
    await asyncio.wait_for(
        runner._send_kanban_notification(adapter, sub, "direct", {}, event=ev, task=None),
        timeout=3,
    )
    await _drain_wakes(runner)

    assert seen.get("any_locked") is False
    assert adapter.calls and adapter.calls[0][1] == "reply"


# ---------------------------------------------------------------------------
# Non-blocking wake dispatch + background delivery accounting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesize_wake_is_non_blocking(monkeypatch):
    """The wake runs a full front-desk agent turn; the notifier must NOT block
    on it. _send_kanban_notification dispatches the wake in the background and
    returns immediately, so the watcher tick is free even while the turn runs.
    Regression for: a long front-desk turn stalling every other delivery."""
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    sub = {"task_id": "t_nb", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "handoff"}, run_id=1)

    gate = asyncio.Event()
    started = asyncio.Event()

    async def blocked_handle_message(event):
        started.set()
        await gate.wait()  # turn does not finish until the test allows it
        return "reply after unblock"

    monkeypatch.setattr(runner, "_handle_message", blocked_handle_message)

    # Returns promptly even though the wake turn is still in-flight (gate unset).
    await asyncio.wait_for(
        runner._send_kanban_notification(
            adapter, sub, "direct fallback", {}, event=ev, task=None,
        ),
        timeout=1,
    )
    await asyncio.wait_for(started.wait(), timeout=1)  # bg wake is actually running
    assert adapter.calls == []  # ...but nothing delivered yet — it is still blocked

    # Let the turn finish; the background task delivers the synthesized reply.
    gate.set()
    await _drain_wakes(runner)
    assert adapter.calls == [("123", "reply after unblock", None)]


@pytest.mark.asyncio
async def test_wake_registered_in_background_tasks_and_completes(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    sub = {"task_id": "t_bg", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "h"}, run_id=1)

    async def fake_handle_message(event):
        return "r"

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)

    await runner._send_kanban_notification(adapter, sub, "fallback", {}, event=ev, task=None)

    # The wake is tracked so it is not GC'd / can be drained on shutdown.
    assert getattr(runner, "_background_tasks", None)
    assert len(runner._background_tasks) == 1

    await _drain_wakes(runner)
    assert all(t.done() for t in runner._background_tasks)


@pytest.mark.asyncio
async def test_wake_total_failure_rewinds_cursor_for_retry(monkeypatch):
    """If the wake turn fails AND the direct fallback send also fails, the
    background task counts the failure and rewinds the claim cursor so a later
    notifier tick retries — preserving the synchronous direct path's retry."""
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    runner.adapters = {}
    # Fallback send also fails -> _wake_with_fallback raises (total failure).
    adapter = _Adapter(SendResult(success=False, error="dead chat"))
    sub = {"task_id": "t_rt", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "h"}, run_id=1)

    async def boom(event):
        raise RuntimeError("wake down")

    monkeypatch.setattr(runner, "_handle_message", boom)
    rewinds = []
    unsubs = []
    monkeypatch.setattr(
        runner, "_kanban_rewind",
        lambda sub, claimed, old, board=None: rewinds.append((sub["task_id"], claimed, old, board)),
    )
    monkeypatch.setattr(
        runner, "_kanban_unsub",
        lambda sub, board=None: unsubs.append((sub["task_id"], board)),
    )
    fail_counts: dict = {}
    sub_key = ("t_rt", "telegram", "123", "")

    await runner._send_kanban_notification(
        adapter, sub, "direct line", {}, event=ev, task=None,
        claimed_cursor=5, old_cursor=2, sub_key=sub_key,
        sub_fail_counts=fail_counts, max_failures=3,
    )
    await _drain_wakes(runner)

    assert fail_counts[sub_key] == 1
    assert rewinds == [("t_rt", 5, 2, None)]
    assert unsubs == []  # not dropped yet — below the max


@pytest.mark.asyncio
async def test_wake_drops_subscription_after_max_failures(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    runner.adapters = {}
    adapter = _Adapter(SendResult(success=False, error="dead chat"))
    sub = {"task_id": "t_drop", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "h"}, run_id=1)

    async def boom(event):
        raise RuntimeError("wake down")

    monkeypatch.setattr(runner, "_handle_message", boom)
    rewinds = []
    unsubs = []
    monkeypatch.setattr(
        runner, "_kanban_rewind",
        lambda sub, claimed, old, board=None: rewinds.append(sub["task_id"]),
    )
    monkeypatch.setattr(
        runner, "_kanban_unsub",
        lambda sub, board=None: unsubs.append((sub["task_id"], board)),
    )
    sub_key = ("t_drop", "telegram", "123", "")
    fail_counts = {sub_key: 2}  # one away from the max

    await runner._send_kanban_notification(
        adapter, sub, "direct line", {}, event=ev, task=None,
        claimed_cursor=5, old_cursor=2, sub_key=sub_key,
        sub_fail_counts=fail_counts, max_failures=3,
    )
    await _drain_wakes(runner)

    assert unsubs == [("t_drop", None)]  # dropped at the max
    assert rewinds == []  # no retry once dropped
    assert sub_key not in fail_counts  # counter cleared with the sub


@pytest.mark.asyncio
async def test_wake_success_unsubscribes_terminal_task(monkeypatch):
    """On a successful wake for a done/archived task, the background task is what
    unsubscribes (and resets the failure counter) — the watcher defers that so a
    failed wake can still retry against the live subscription."""
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    sub = {"task_id": "t_done", "platform": "telegram", "chat_id": "123"}
    ev = SimpleNamespace(kind="completed", payload={"summary": "h"}, run_id=1)
    task = SimpleNamespace(status="done", assignee="worker", title="T", result=None)

    async def fake_handle_message(event):
        return "done reply"

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)
    unsubs = []
    monkeypatch.setattr(
        runner, "_kanban_unsub",
        lambda sub, board=None: unsubs.append((sub["task_id"], board)),
    )
    sub_key = ("t_done", "telegram", "123", "")
    fail_counts = {sub_key: 1}

    await runner._send_kanban_notification(
        adapter, sub, "fallback", {}, event=ev, task=task,
        claimed_cursor=3, old_cursor=1, sub_key=sub_key,
        sub_fail_counts=fail_counts, max_failures=3,
    )
    await _drain_wakes(runner)

    assert adapter.calls == [("123", "done reply", None)]
    assert unsubs == [("t_done", None)]
    assert sub_key not in fail_counts  # reset on success


# ---------------------------------------------------------------------------
# Direct-mode mirroring + per-origin lock serialization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_notification_mirrors_into_origin_session(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "direct")
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_ctx", "platform": "telegram", "chat_id": "123",
        "thread_id": "7", "user_id": "u1",
    }
    mirror_calls = []

    def fake_mirror(platform, chat_id, message_text, source_label="cli", thread_id=None, user_id=None):
        mirror_calls.append({
            "platform": platform, "chat_id": chat_id, "message_text": message_text,
            "source_label": source_label, "thread_id": thread_id, "user_id": user_id,
        })
        return True

    monkeypatch.setattr("gateway.mirror.mirror_to_session", fake_mirror)

    await runner._send_kanban_notification(
        adapter, sub, "done text", {"thread_id": "7"},
        event=SimpleNamespace(kind="completed"), task=None,
    )

    assert adapter.calls == [("123", "done text", {"thread_id": "7"})]
    assert mirror_calls == [{
        "platform": "telegram", "chat_id": "123", "message_text": "done text",
        "source_label": "kanban", "thread_id": "7", "user_id": "u1",
    }]


@pytest.mark.asyncio
async def test_wake_serializes_via_handle_message_lock(monkeypatch):
    """Serialization against the active origin turn is preserved — but it lives
    INSIDE the wake's ``_handle_message`` (which acquires the per-session lock),
    NOT in the notifier. The notifier itself no longer blocks: it dispatches the
    wake in the background and returns immediately, so the wait-on-the-held-lock
    happens in the background task. Nothing is delivered until the lock frees;
    then the queued wake proceeds. (On the old inline-await code the notifier
    held the lock around the wake, so this same turn would self-deadlock.)"""
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    sub = {
        "task_id": "t_wait", "platform": "telegram", "chat_id": "123",
        "thread_id": "7", "user_id": "u1",
    }
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1", thread_id="7")
    lock = runner._conversation_lock_for_source(source)
    await lock.acquire()
    wake_done = asyncio.Event()

    async def fake_handle_message(event):
        # Mirror the real agent turn: acquire the same per-session lock.
        async with runner._conversation_lock_for_source(source):
            wake_done.set()
            return "synth after native"

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)

    # The notifier call returns immediately even while the lock is held — it is
    # NOT blocked by the front-desk turn.
    await asyncio.wait_for(
        runner._send_kanban_notification(
            adapter, sub, "direct fallback", {"thread_id": "7"},
            event=SimpleNamespace(kind="completed", payload={"summary": "h"}),
        ),
        timeout=2,
    )
    await asyncio.sleep(0)

    # The background wake's turn is waiting on the held lock — nothing delivered.
    assert adapter.calls == []
    assert not wake_done.is_set()

    lock.release()
    await _drain_wakes(runner)

    assert wake_done.is_set()
    assert adapter.calls == [("123", "synth after native", {"thread_id": "7"})]


@pytest.mark.asyncio
async def test_notification_does_not_block_unrelated_chats(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "direct")
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    blocked_adapter = _Adapter(SendResult(success=True, message_id="blocked"))
    free_adapter = _Adapter(SendResult(success=True, message_id="free"))
    blocked_sub = {"task_id": "t_blocked", "platform": "telegram", "chat_id": "123", "user_id": "u1"}
    free_sub = {"task_id": "t_free", "platform": "telegram", "chat_id": "456", "user_id": "u2"}
    blocked_source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    lock = runner._conversation_lock_for_source(blocked_source)
    await lock.acquire()
    monkeypatch.setattr("gateway.mirror.mirror_to_session", lambda *a, **k: True)

    blocked_task = asyncio.create_task(
        runner._send_kanban_notification(blocked_adapter, blocked_sub, "blocked", {})
    )
    await asyncio.sleep(0)
    await runner._send_kanban_notification(free_adapter, free_sub, "free", {})

    assert blocked_adapter.calls == []
    assert free_adapter.calls == [("456", "free", {})]

    lock.release()
    await asyncio.wait_for(blocked_task, timeout=1)


@pytest.mark.asyncio
async def test_send_failure_does_not_append_to_session(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=False, error="send failed"))
    sub = {"task_id": "t_fail_mirror", "platform": "telegram", "chat_id": "123"}
    mirror_calls = []
    monkeypatch.setattr(
        "gateway.mirror.mirror_to_session",
        lambda *args, **kwargs: mirror_calls.append((args, kwargs)) or True,
    )

    with pytest.raises(RuntimeError, match="send failed"):
        await runner._send_kanban_notification(adapter, sub, "will not mirror", {})

    assert mirror_calls == []


# ---------------------------------------------------------------------------
# Gateway FIFO queue + notifier ownership (unchanged behavior)
# ---------------------------------------------------------------------------

def test_gateway_queue_fifo_preserves_two_user_messages():
    runner = object.__new__(GatewayRunner)
    runner._queued_events = {}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    session_key = "telegram:123:u1"
    adapter = SimpleNamespace(_pending_messages={})
    first = MessageEvent(text="first", message_type=MessageType.TEXT, source=source)
    second = MessageEvent(text="second", message_type=MessageType.TEXT, source=source)

    runner._enqueue_fifo(session_key, first, adapter)
    runner._enqueue_fifo(session_key, second, adapter)

    pending = adapter._pending_messages.pop(session_key)
    assert pending.text == "first"
    promoted = runner._promote_queued_event(session_key, adapter, None)
    assert promoted.text == "second"


def test_kanban_notifier_defaults_to_dispatch_owner(monkeypatch):
    """Secondary gateways with dispatch disabled must not consume shared subs."""
    from hermes_cli import config as hermes_config

    runner = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", raising=False)
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )

    assert runner._kanban_notify_in_gateway_enabled() is False


def test_kanban_notifier_explicit_config_overrides_dispatch(monkeypatch):
    from hermes_cli import config as hermes_config

    runner = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", raising=False)
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False, "notify_in_gateway": True}},
    )

    assert runner._kanban_notify_in_gateway_enabled() is True


def test_kanban_notifier_env_override(monkeypatch):
    from hermes_cli import config as hermes_config

    runner = object.__new__(GatewayRunner)
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )

    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", "false")
    assert runner._kanban_notify_in_gateway_enabled() is False

    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", "true")
    assert runner._kanban_notify_in_gateway_enabled() is True


# ---------------------------------------------------------------------------
# End-to-end watcher tick: real board DB, synthesize completion -> wake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watcher_dispatches_wake_without_artifact_double_upload(monkeypatch, tmp_path):
    """One real notifier tick over a seeded board: a completed task in synthesize
    mode dispatches the origin wake in the background (non-blocking), the watcher
    does NOT also upload artifacts (the woken agent owns them), and the terminal
    subscription is unsubscribed by the background task once the wake succeeds —
    not synchronously by the watcher (so a failed wake could still retry)."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_MODE", "synthesize")
    kb.init_db()

    artifact = tmp_path / "report.txt"
    artifact.write_text("deliverable")

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="investigate X", assignee="orchestrator")
        kb.recompute_ready(conn)
        kb.complete_task(conn, tid, summary=f"All done. See {artifact}")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="123", user_id="u1",
        )

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._conversation_locks = {}
    runner._kanban_notifier_profile = "default"
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, "_kanban_notify_in_gateway_enabled", lambda: True)

    async def fake_handle_message(event):
        return "Here's what I found."

    monkeypatch.setattr(runner, "_handle_message", fake_handle_message)

    artifact_calls = []

    async def record_artifacts(**kwargs):
        artifact_calls.append(kwargs)

    monkeypatch.setattr(runner, "_deliver_kanban_artifacts", record_artifacts)

    # No real waiting: skip the watcher's startup delay + inter-tick sleeps.
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr("gateway.kanban_notifier.asyncio.sleep", fast_sleep)

    # Stop the watcher after it has processed the first delivery, so it doesn't
    # busy-spin once sleeps are no-ops.
    real_send = runner._send_kanban_notification

    async def stopping_send(*args, **kwargs):
        await real_send(*args, **kwargs)
        runner._running = False

    monkeypatch.setattr(runner, "_send_kanban_notification", stopping_send)

    watcher = asyncio.create_task(runner._kanban_notifier_watcher(interval=1))
    try:
        await asyncio.wait_for(watcher, timeout=5)
    finally:
        runner._running = False
        if not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    # The background wake delivers the synthesized reply to the origin chat.
    await _drain_wakes(runner)
    assert adapter.calls == [("123", "Here's what I found.", None)]
    # Artifact upload is the woken agent's job in synthesize mode — the watcher
    # must not double-deliver it.
    assert artifact_calls == []
    # The done task's subscription is gone — unsubscribed by the wake task.
    with kb.connect() as conn:
        assert kb.list_notify_subs(conn) == []
