"""Kanban completion notification delivery (local Hermes patch).

Upstream's ``_kanban_notifier_watcher`` only sends the raw worker handoff.
This module adds the ``notification_mode`` delivery policy:

* ``direct`` — send the worker handoff/status line to the origin chat.
* ``synthesize`` — re-enter the handoff into the *origin session* as a
  synthetic inbound turn (``_wake_origin_session``) so the origin profile's
  normal agent loop composes and delivers the user-facing reply. The rewrite
  is an ordinary agent turn, not a second LLM path inside the gateway notifier
  — the design teknium1 asked for when closing PR #21523.
* ``silent`` — suppress delivery (internal fan-out children).

Why a mixin: ``_send_kanban_notification`` / ``_wake_origin_session`` reach
GatewayRunner instance state (``_conversation_lock_for_source``,
``adapters``, ``_handle_message``) and tests/callers reach them via
``GatewayRunner``. Inheriting via ``class GatewayRunner(KanbanSynthesisMixin)``
preserves the call shapes without re-binding tricks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional


logger = logging.getLogger(__name__)


class KanbanSynthesisMixin:
    """Delivery + origin-wake helpers for Kanban completion notifications.

    Depends on these ``GatewayRunner`` attributes/methods, provided by the
    concrete class:

    * ``_conversation_lock_for_source(source) -> asyncio.Lock``
    * ``adapters`` — connected platform adapters
    * ``_handle_message(MessageEvent) -> Optional[str]`` — the normal agent
      loop entry point (also used by ``_process_handoff``)
    """

    async def _send_kanban_notification(
        self,
        adapter: Any,
        sub: dict,
        msg: str,
        metadata: "dict[str, Any]",
        *,
        event: Any = None,
        task: Any = None,
        board: Optional[str] = None,
        claimed_cursor: Optional[int] = None,
        old_cursor: int = 0,
        sub_key: Optional[tuple] = None,
        sub_fail_counts: Optional[dict] = None,
        max_failures: int = 3,
    ) -> None:
        """Send one Kanban notification and raise if the adapter reports failure.

        Platform adapters normally return ``SendResult`` instead of raising on
        delivery failure. The notifier must treat ``success=False`` as failure;
        otherwise it advances/unsubscribes and silently loses the completion
        ping even though nothing reached the user. ``notification_mode`` can
        opt into synthesized user-facing text or suppress delivery entirely.
        """
        # Delivery mode is an operator policy resolved from config per platform
        # (see _resolve_kanban_notify_mode), NOT the per-task sub column — the
        # column is vestigial for delivery. This removes the footgun of a model
        # setting 'silent' on a user-visible task.
        platform_str = str(sub.get("platform") or "telegram").lower()
        mode = self._resolve_kanban_notify_mode(platform_str)
        if mode == "silent":
            logger.info(
                "kanban notifier: silent notification for %s on %s:%s",
                sub["task_id"], sub["platform"], sub["chat_id"],
            )
            return
        send_msg = msg

        def _record_gateway_span(name: str, **kwargs: Any) -> None:
            try:
                from agent.telemetry import record_span_event

                attrs = dict(kwargs.pop("attributes", {}) or {})
                attrs.setdefault("platform", str(sub.get("platform") or ""))
                attrs.setdefault("notification_mode", mode)
                attrs.setdefault("task_id", str(sub.get("task_id") or ""))
                if sub.get("request_id"):
                    attrs.setdefault("request_id", str(sub.get("request_id")))
                if getattr(event, "kind", None):
                    attrs.setdefault("event_kind", str(getattr(event, "kind")))
                if task and getattr(task, "assignee", None):
                    attrs.setdefault("profile", str(getattr(task, "assignee")))
                if task and getattr(task, "status", None):
                    attrs.setdefault("task_status", str(getattr(task, "status")))
                record_span_event(
                    name,
                    platform="gateway",
                    profile=str(getattr(task, "assignee", "") or ""),
                    attributes=attrs,
                    **kwargs,
                )
            except Exception:
                pass

        # Synthesize completion: re-enter the worker handoff into the origin
        # session via the normal agent loop. The wake runs a FULL front-desk
        # agent turn (``_handle_message`` under the per-session conversation
        # lock). Awaiting it here would block the ~5s notifier-watcher tick for
        # the entire turn — every other board/subscription/heartbeat delivery
        # stalls behind one chat's LLM turn, and a turn longer than the wake
        # timeout degrades to a raw direct send. So dispatch it as a tracked
        # background task and return immediately; the watcher tick continues.
        #
        # The DB cursor was already advanced at claim time
        # (``claim_unseen_events_for_sub``), so dedup holds and the next tick
        # won't re-deliver this event. The background task owns the delivery
        # OUTCOME: on success it unsubscribes a terminal task and resets the
        # failure counter; on total failure (wake AND direct fallback both fail)
        # it rewinds the cursor for retry / drops a dead subscription — the same
        # accounting the watcher does for the synchronous direct path. (The wake
        # runs OUTSIDE the conversation lock — ``_handle_message`` acquires it
        # itself; wrapping it would self-deadlock.)
        if mode == "synthesize" and getattr(event, "kind", None) == "completed":
            self._register_background_task(
                asyncio.ensure_future(
                    self._run_kanban_wake_delivery(
                        sub=sub, event=event, task=task, board=board,
                        msg=msg, metadata=metadata, adapter=adapter,
                        claimed_cursor=claimed_cursor, old_cursor=old_cursor,
                        sub_key=sub_key, sub_fail_counts=sub_fail_counts,
                        max_failures=max_failures,
                    )
                )
            )
            _record_gateway_span("kanban.origin_session_wake_dispatched")
            return

        async def _send_and_mirror() -> None:
            nonlocal send_msg
            adapter_started = time.monotonic()
            adapter_status = "ok"
            adapter_error = None
            try:
                result = await adapter.send(sub["chat_id"], send_msg, metadata=metadata)
                if getattr(result, "success", True) is False:
                    adapter_status = "error"
                    adapter_error = getattr(result, "error", None) or "adapter returned success=False"
                    raise RuntimeError(str(adapter_error))
                _record_gateway_span("kanban.final_notification_sent")
            except Exception as exc:
                adapter_status = "error"
                adapter_error = str(exc)
                raise
            finally:
                _record_gateway_span(
                    f"adapter.send.{str(sub.get('platform') or 'unknown').lower()}",
                    duration_ms=(time.monotonic() - adapter_started) * 1000,
                    status=adapter_status,
                    error=adapter_error,
                )

            try:
                from gateway.mirror import mirror_to_session

                mirrored = mirror_to_session(
                    str(sub.get("platform") or ""),
                    str(sub.get("chat_id") or ""),
                    send_msg,
                    source_label="kanban",
                    thread_id=(str(sub.get("thread_id") or "") or None),
                    user_id=(str(sub.get("user_id") or "") or None),
                )
            except Exception:
                mirrored = False

            logger.info(
                "kanban notifier: sent %s event to %s:%s message_id=%s mirrored=%s",
                sub["task_id"], sub["platform"], sub["chat_id"],
                getattr(result, "message_id", None), mirrored,
            )

        try:
            from gateway.config import Platform as _Platform
            from gateway.session import SessionSource

            platform_str = str(sub.get("platform") or "telegram").lower()
            platform = _Platform(platform_str)
            source = SessionSource(
                platform=platform,
                chat_id=str(sub.get("chat_id") or ""),
                user_id=str(sub.get("user_id") or "") or None,
                thread_id=str(sub.get("thread_id") or "") or None,
            )
            lock = self._conversation_lock_for_source(source)
        except Exception:
            lock = None

        gateway_started = time.monotonic()
        gateway_status = "ok"
        gateway_error = None
        try:
            if lock is None:
                await _send_and_mirror()
            else:
                async with lock:
                    await _send_and_mirror()
        except Exception as exc:
            gateway_status = "error"
            gateway_error = str(exc)
            raise
        finally:
            _record_gateway_span(
                "gateway.send",
                duration_ms=(time.monotonic() - gateway_started) * 1000,
                status=gateway_status,
                error=gateway_error,
            )

    async def _wake_origin_session(
        self,
        *,
        sub: dict,
        event: Any,
        task: Any,
        board: Optional[str],
        direct_message: str,
    ) -> None:
        """Re-enter a completed task's handoff into its origin session.

        Replaces gateway-side synthesis. Builds a synthetic *internal* user
        turn carrying the worker handoff and dispatches it through the normal
        agent loop (``_handle_message``) — exactly the pattern proven by
        ``_process_handoff``. The origin profile composes and delivers the
        user-facing reply itself, with full tool access (it can read referenced
        artifact files), so there is no second LLM/rendering path baked into the
        gateway notifier.
        """
        from gateway.config import Platform as _Platform
        from gateway.session import SessionSource
        from gateway.platforms.base import MessageEvent

        platform_str = str(sub.get("platform") or "telegram").lower()
        try:
            platform = _Platform(platform_str)
        except Exception:
            platform = _Platform.TELEGRAM
        source = SessionSource(
            platform=platform,
            chat_id=str(sub.get("chat_id") or ""),
            user_id=str(sub.get("user_id") or "") or None,
            thread_id=str(sub.get("thread_id") or "") or None,
        )

        event_payload = getattr(event, "payload", None) or {}
        handoff = (
            event_payload.get("summary")
            or (task.result if task and getattr(task, "result", None) else None)
            or direct_message
        )
        origin_context = str(sub.get("origin_context") or "").strip()
        synthetic_text = (
            "[A background task you delegated has just completed. Reply to the "
            "user with the consolidated result. Hide internal task plumbing "
            "(task ids, workers, dispatcher, board names) unless the user asked "
            "for internals/debugging. You may read referenced artifact files "
            "with your tools if you need more detail than the handoff gives.\n\n"
            + (f"Original request/context:\n{origin_context[:2000]}\n\n" if origin_context else "")
            + f"Worker handoff:\n{str(handoff or '').strip()}]"
        )

        synthetic_event = MessageEvent(
            text=synthetic_text,
            source=source,
            internal=True,
        )
        response_text = await self._handle_message(synthetic_event)
        if not response_text:
            # Streaming path already delivered the reply to the origin chat.
            return
        adapter = self.adapters.get(platform)
        if adapter is None:
            return
        metadata: dict[str, Any] = {}
        if sub.get("thread_id"):
            metadata["thread_id"] = str(sub["thread_id"])
        await adapter.send(
            str(sub.get("chat_id") or ""), response_text, metadata=metadata or None,
        )


    @staticmethod
    def _resolve_kanban_notify_mode(
        platform: str, user_config: Optional[dict] = None,
    ) -> str:
        """Resolve completion-delivery mode for a platform — operator policy.

        Precedence: ``HERMES_KANBAN_NOTIFY_MODE`` env (global) >
        ``kanban.notify.<platform>.mode`` > ``kanban.notify.mode`` > a built-in
        default that preserves the prior per-task default (interactive Telegram
        → ``synthesize``, everything else → ``direct``). Unknown values are
        ignored. Only ``direct`` / ``synthesize`` / ``silent`` are valid.

        The notifier only ever calls this for *connected gateway adapters*, so
        the platform is always wakeable — no CLI/cron clamp is needed here (CLI
        origins are never iterated by the watcher).
        """
        valid = {"direct", "synthesize", "silent"}
        env = (os.environ.get("HERMES_KANBAN_NOTIFY_MODE") or "").strip().lower()
        if env in valid:
            return env
        cfg = user_config
        if cfg is None:
            try:
                from gateway.run import _load_gateway_config
                cfg = _load_gateway_config()
            except Exception:
                cfg = {}
        notify = {}
        if isinstance(cfg, dict):
            kanban_cfg = cfg.get("kanban")
            if isinstance(kanban_cfg, dict) and isinstance(kanban_cfg.get("notify"), dict):
                notify = kanban_cfg["notify"]
        per_platform = notify.get(platform) if isinstance(notify, dict) else None
        if isinstance(per_platform, dict):
            m = str(per_platform.get("mode") or "").strip().lower()
            if m in valid:
                return m
        m = str(notify.get("mode") or "").strip().lower() if isinstance(notify, dict) else ""
        if m in valid:
            return m
        # Built-in default: preserve the prior per-task default so deploy does
        # not silently regress when no kanban.notify config is present.
        return "synthesize" if platform == "telegram" else "direct"

    @staticmethod
    def _kanban_wake_timeout(user_config: Optional[dict] = None) -> float:
        """Bounded wall-clock cap for a single origin-session wake turn."""
        cfg = user_config
        if cfg is None:
            try:
                from gateway.run import _load_gateway_config
                cfg = _load_gateway_config()
            except Exception:
                cfg = {}
        raw = 180
        if isinstance(cfg, dict):
            kanban_cfg = cfg.get("kanban")
            if isinstance(kanban_cfg, dict):
                notify = kanban_cfg.get("notify")
                if isinstance(notify, dict) and notify.get("wake_timeout_seconds") is not None:
                    raw = notify.get("wake_timeout_seconds")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 180.0
        return max(10.0, min(value, 600.0))

    async def _wake_with_fallback(
        self,
        *,
        sub: dict,
        event: Any,
        task: Any,
        board: Optional[str],
        msg: str,
        metadata: "dict[str, Any]",
        adapter: Any,
    ) -> None:
        """Wake the origin session, bounded by a timeout; fall back to direct.

        Guarantees the user gets *something*: if the agent turn errors or
        exceeds the wake timeout, deliver the direct status line (``msg``)
        instead. Raises only if the fallback adapter send also fails, so the
        notifier's existing failure/retry path still applies in that case.
        """
        timeout_s = self._kanban_wake_timeout()
        try:
            await asyncio.wait_for(
                self._wake_origin_session(
                    sub=sub, event=event, task=task, board=board,
                    direct_message=msg,
                ),
                timeout=timeout_s,
            )
            return
        except Exception as exc:
            is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            logger.warning(
                "kanban notifier: origin-session wake failed for %s "
                "(timeout=%s); falling back to direct send: %s",
                sub.get("task_id"), is_timeout, exc,
            )
        result = await adapter.send(sub["chat_id"], msg, metadata=metadata)
        if getattr(result, "success", True) is False:
            raise RuntimeError(
                getattr(result, "error", None) or "fallback adapter send failed"
            )

    def _register_background_task(self, task: "asyncio.Future") -> "asyncio.Future":
        """Track a fire-and-forget task so it isn't GC'd and shuts down cleanly.

        Mirrors GatewayRunner's existing ``_background_tasks`` pattern (add +
        discard-on-done). Created lazily so callers that bypass ``__init__``
        (e.g. ``object.__new__(GatewayRunner)`` in tests) still work.
        """
        tasks = getattr(self, "_background_tasks", None)
        if tasks is None:
            tasks = set()
            self._background_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _run_kanban_wake_delivery(
        self,
        *,
        sub: dict,
        event: Any,
        task: Any,
        board: Optional[str],
        msg: str,
        metadata: "dict[str, Any]",
        adapter: Any,
        claimed_cursor: Optional[int] = None,
        old_cursor: int = 0,
        sub_key: Optional[tuple] = None,
        sub_fail_counts: Optional[dict] = None,
        max_failures: int = 3,
    ) -> None:
        """Run the origin-session wake off the notifier tick + own its outcome.

        The notifier dispatches this in the background so its watcher loop is
        not blocked by the front-desk agent turn. Because the watcher therefore
        cannot observe the delivery result synchronously, the cursor / failure /
        unsubscribe accounting it normally performs for the direct path moves
        here:

        * **success** — reset the per-sub failure counter and unsubscribe the
          row when the task reached a terminal status (``done``/``archived``).
          The cursor is already at the claimed position, so nothing to advance.
        * **total failure** (``_wake_with_fallback`` raised — both the wake turn
          and the direct fallback send failed) — count the failure and, after
          ``max_failures`` consecutive ones, drop the dead subscription;
          otherwise rewind the cursor so a later tick retries.

        The accounting is skipped gracefully when the optional context
        (``sub_key`` / ``sub_fail_counts`` / ``claimed_cursor``) is absent, so
        direct callers and unit tests get a plain non-blocking wake.
        """
        try:
            await self._wake_with_fallback(
                sub=sub, event=event, task=task, board=board,
                msg=msg, metadata=metadata, adapter=adapter,
            )
        except Exception as exc:
            fails = 0
            if sub_fail_counts is not None and sub_key is not None:
                fails = sub_fail_counts.get(sub_key, 0) + 1
                sub_fail_counts[sub_key] = fails
            logger.warning(
                "kanban notifier: origin-session wake delivery failed for %s "
                "(attempt %d/%d): %s",
                sub.get("task_id"), fails, max_failures, exc,
            )
            if (
                sub_fail_counts is not None
                and sub_key is not None
                and fails >= max_failures
            ):
                await asyncio.to_thread(self._kanban_unsub, sub, board)
                sub_fail_counts.pop(sub_key, None)
            elif claimed_cursor is not None:
                await asyncio.to_thread(
                    self._kanban_rewind, sub, claimed_cursor, old_cursor, board,
                )
            return

        if sub_fail_counts is not None and sub_key is not None:
            sub_fail_counts.pop(sub_key, None)
        if task is not None and getattr(task, "status", None) in {"done", "archived"}:
            await asyncio.to_thread(self._kanban_unsub, sub, board)


__all__ = ["KanbanSynthesisMixin"]
