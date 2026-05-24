import asyncio
import logging
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
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_metrics",
        "platform": "telegram",
        "chat_id": "123",
        "notification_mode": "direct",
    }
    task = SimpleNamespace(assignee="worker", status="done")
    event = SimpleNamespace(kind="completed")
    spans = []

    def fake_record_span_event(name, **kwargs):
        spans.append((name, kwargs))

    monkeypatch.setattr("agent.telemetry.record_span_event", fake_record_span_event)
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


@pytest.mark.asyncio
async def test_kanban_notification_synthesize_mode_sends_synthesized_text(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_syn",
        "platform": "telegram",
        "chat_id": "123",
        "notification_mode": "synthesize",
        "origin_context": "user asked for a concise result",
    }
    ev = SimpleNamespace(kind="completed", payload={"summary": "worker handoff"}, run_id=7)

    async def fake_synthesize(**kwargs):
        assert kwargs["sub"] is sub
        assert kwargs["event"] is ev
        assert kwargs["direct_message"] == "direct fallback"
        return "synthesized reply"

    monkeypatch.setattr(runner, "_synthesize_kanban_notification", fake_synthesize)

    await runner._send_kanban_notification(
        adapter, sub, "direct fallback", {}, event=ev, task=None, board="default"
    )

    assert adapter.calls == [("123", "synthesized reply", {})]


@pytest.mark.asyncio
async def test_kanban_notification_synthesis_failure_uses_public_fallback(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_syn_fail",
        "platform": "telegram",
        "chat_id": "123",
        "notification_mode": "synthesize",
    }
    ev = SimpleNamespace(kind="completed", payload={"summary": "worker handoff"}, run_id=7)

    async def fake_synthesize(**kwargs):
        raise RuntimeError("synthesis unavailable")

    monkeypatch.setattr(runner, "_synthesize_kanban_notification", fake_synthesize)

    await runner._send_kanban_notification(
        adapter, sub, "direct fallback", {"thread_id": "9"}, event=ev, task=None
    )

    assert adapter.calls == [("123", "handoff", {"thread_id": "9"})]


@pytest.mark.asyncio
async def test_kanban_notification_silent_mode_noops():
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_silent",
        "platform": "telegram",
        "chat_id": "123",
        "notification_mode": "silent",
    }

    await runner._send_kanban_notification(adapter, sub, "direct fallback", {})

    assert adapter.calls == []


@pytest.mark.asyncio
async def test_kanban_notification_success_mirrors_into_origin_session(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_ctx",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "7",
        "user_id": "u1",
    }
    mirror_calls = []

    def fake_mirror(platform, chat_id, message_text, source_label="cli", thread_id=None, user_id=None):
        mirror_calls.append({
            "platform": platform,
            "chat_id": chat_id,
            "message_text": message_text,
            "source_label": source_label,
            "thread_id": thread_id,
            "user_id": user_id,
        })
        return True

    monkeypatch.setattr("gateway.mirror.mirror_to_session", fake_mirror)

    await runner._send_kanban_notification(
        adapter, sub, "done text", {"thread_id": "7"}
    )

    assert adapter.calls == [("123", "done text", {"thread_id": "7"})]
    assert mirror_calls == [{
        "platform": "telegram",
        "chat_id": "123",
        "message_text": "done text",
        "source_label": "kanban",
        "thread_id": "7",
        "user_id": "u1",
    }]


@pytest.mark.asyncio
async def test_kanban_synthesis_waits_for_active_origin_session_lock(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._conversation_locks = {}
    adapter = _Adapter(SendResult(success=True, message_id="m1"))
    sub = {
        "task_id": "t_wait",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "7",
        "user_id": "u1",
        "notification_mode": "synthesize",
    }
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u1",
        thread_id="7",
    )
    lock = runner._conversation_lock_for_source(source)
    await lock.acquire()
    synth_started = asyncio.Event()

    async def fake_synthesize(**kwargs):
        synth_started.set()
        return "synth after native"

    monkeypatch.setattr(runner, "_synthesize_kanban_notification", fake_synthesize)
    monkeypatch.setattr("gateway.mirror.mirror_to_session", lambda *a, **k: True)

    task = asyncio.create_task(
        runner._send_kanban_notification(
            adapter,
            sub,
            "direct fallback",
            {"thread_id": "7"},
            event=SimpleNamespace(kind="completed"),
        )
    )
    await asyncio.sleep(0)

    assert adapter.calls == []
    assert not synth_started.is_set()

    lock.release()
    await asyncio.wait_for(task, timeout=1)

    assert adapter.calls == [("123", "synth after native", {"thread_id": "7"})]


@pytest.mark.asyncio
async def test_kanban_notification_does_not_block_unrelated_chats(monkeypatch):
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
async def test_kanban_notification_send_failure_does_not_append_to_session(monkeypatch):
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
        hermes_config,
        "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False}},
    )

    assert runner._kanban_notify_in_gateway_enabled() is False


def test_kanban_notifier_explicit_config_overrides_dispatch(monkeypatch):
    from hermes_cli import config as hermes_config

    runner = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", raising=False)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": False, "notify_in_gateway": True}},
    )

    assert runner._kanban_notify_in_gateway_enabled() is True


def test_kanban_notifier_env_override(monkeypatch):
    from hermes_cli import config as hermes_config

    runner = object.__new__(GatewayRunner)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )

    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", "false")
    assert runner._kanban_notify_in_gateway_enabled() is False

    monkeypatch.setenv("HERMES_KANBAN_NOTIFY_IN_GATEWAY", "true")
    assert runner._kanban_notify_in_gateway_enabled() is True


def test_kanban_synthesis_prompt_hides_internal_workflow_by_default():
    task = SimpleNamespace(
        title="answer factual question",
        body="User asked: how many airports does Guangzhou have?",
    )
    event = SimpleNamespace(payload={"summary": "Guangzhou has one operating passenger airport."})
    sub = {
        "task_id": "t_internal123",
        "origin_context": "how many airports does Guangzhou have?",
        "origin_session_id": "telegram:chat",
        "origin_profile": "default",
    }

    prompt = GatewayRunner._build_kanban_synthesis_prompt(
        sub=sub,
        event=event,
        task=task,
        board="default",
        worker_summary="Guangzhou has one operating passenger airport.",
        worker_metadata={"assignee": "worker-research"},
        direct_message="✔ @worker-research Kanban t_internal123 done — answer factual question",
    )

    assert "Do NOT mention Kanban" in prompt
    assert "task ids" in prompt
    assert "assignees" in prompt
    assert "worker/dispatcher" in prompt
    assert "Internal debug context" in prompt
    assert "t_internal123" in prompt  # available for debug context, not for normal prose


def test_kanban_synthesis_prompt_includes_safe_artifact_excerpt(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    # The implementation resolves allowed roots via ~/.hermes, so create the
    # same shape under tmp_path and point metadata at it.
    allowed_workspace = tmp_path / ".hermes" / "kanban" / "workspaces" / "t_report"
    allowed_workspace.mkdir(parents=True)
    allowed_report = allowed_workspace / "report.md"
    allowed_report.write_text(
        "# Actual report\n\nSubstantive content, not just a path.",
        encoding="utf-8",
    )

    task = SimpleNamespace(workspace_path=str(allowed_workspace))
    excerpt = GatewayRunner._read_kanban_artifact_context(
        task=task,
        worker_metadata={"artifact_path": str(allowed_report)},
    )

    assert "Actual report" in excerpt
    assert "Substantive content" in excerpt


def test_kanban_public_fallback_hides_internal_plumbing_and_paths():
    fallback = GatewayRunner._kanban_public_completion_fallback(
        worker_summary=(
            "✔ @worker-code Kanban t_396c8c39 done — Created report at "
            "`/Users/verkyyi/.hermes/kanban/workspaces/t_396c8c39/report.md`"
        ),
        worker_metadata={},
    )

    assert "Kanban" not in fallback
    assert "@worker-code" not in fallback
    assert "t_396c8c39" not in fallback
    assert "/Users/" not in fallback
    assert "generated report" in fallback

def test_kanban_public_fallback_sanitizes_metadata_values():
    fallback = GatewayRunner._kanban_public_completion_fallback(
        worker_summary="",
        worker_metadata={
            "result": (
                "✔ @worker Kanban t_bebc1a4f done from "
                "/Users/verkyyi/.hermes/kanban/workspaces/t_bebc1a4f/out.md"
            )
        },
    )

    assert "Kanban" not in fallback
    assert "@worker" not in fallback
    assert "t_bebc1a4f" not in fallback
    assert "/Users/" not in fallback


def test_kanban_synthesis_timeout_default_and_clamp():
    assert GatewayRunner._kanban_synthesis_timeout({}) == 120
    assert GatewayRunner._kanban_synthesis_timeout({"kanban": {"synthesis_timeout_seconds": 2}}) == 5
    assert GatewayRunner._kanban_synthesis_timeout({"kanban": {"synthesis_timeout_seconds": "90"}}) == 90
    assert GatewayRunner._kanban_synthesis_timeout({"kanban": {"synthesis_timeout_seconds": "bad"}}) == 120


def test_kanban_synthesis_route_prefers_auxiliary_config():
    route = GatewayRunner._kanban_synthesis_route({
        "auxiliary": {
            "kanban_synthesis": {
                "provider": "openrouter",
                "model": "google/gemini-2.5-flash",
                "base_url": "",
            }
        }
    })

    assert route["provider"] == "openrouter"
    assert route["model"] == "google/gemini-2.5-flash"


@pytest.mark.asyncio
async def test_kanban_synthesis_uses_auxiliary_llm_without_agent_memory(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    calls = []

    async def inline_executor(fn):
        return fn()

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="final concise reply"))]
        )

    monkeypatch.setattr(runner, "_run_in_executor_with_context", inline_executor)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "kanban": {"synthesis_timeout_seconds": 120},
        "auxiliary": {"kanban_synthesis": {"provider": "openrouter", "model": "google/gemini-2.5-flash"}},
    })
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **kwargs: ("main-slow-model", {"provider": "openai-codex", "api_key": "secret"}),
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    reply = await runner._synthesize_kanban_notification(
        sub={"platform": "telegram", "chat_id": "123", "task_id": "t_aux"},
        event=SimpleNamespace(id=3, kind="completed", payload={"summary": "worker handoff"}),
        task=SimpleNamespace(title="title", body="body", workspace_path=""),
        board="default",
        direct_message="direct",
    )

    assert reply == "final concise reply"
    assert calls[0]["task"] == "kanban_synthesis"
    assert calls[0]["timeout"] == 120
    assert calls[0]["main_runtime"]["model"] == "main-slow-model"
    assert calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_kanban_synthesis_timeout_fallback_logs_diagnostics(monkeypatch, caplog):
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_db = None
    runner._fallback_model = None

    async def fake_executor(_fn):
        await asyncio.sleep(0.05)
        return "too late"

    monkeypatch.setattr(runner, "_run_in_executor_with_context", fake_executor)
    monkeypatch.setattr(GatewayRunner, "_kanban_synthesis_timeout", staticmethod(lambda _config: 0.01))
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "kanban": {"synthesis_timeout_seconds": 120},
        "auxiliary": {"kanban_synthesis": {"provider": "openrouter", "model": "google/gemini-2.5-flash"}},
    })
    monkeypatch.setattr(
        runner,
        "_resolve_session_agent_runtime",
        lambda **kwargs: ("main-slow-model", {"provider": "openai-codex", "api_key": "secret"}),
    )

    caplog.set_level(logging.WARNING, logger="gateway.run")
    reply = await runner._synthesize_kanban_notification(
        sub={"platform": "telegram", "chat_id": "123", "task_id": "t_timeout"},
        event=SimpleNamespace(id=3, kind="completed", payload={"summary": "done from /Users/me/.hermes/kanban/workspaces/t_timeout/report.md"}),
        task=SimpleNamespace(title="title", body="body", workspace_path=""),
        board="default",
        direct_message="direct",
    )

    assert "t_timeout" not in reply
    assert "/Users/" not in reply
    log_text = caplog.text
    assert "TimeoutError" in log_text
    assert "provider=openrouter" in log_text
    assert "model=google/gemini-2.5-flash" in log_text
    assert "elapsed=" in log_text
    assert "prompt_chars=" in log_text
