"""Kanban completion notification synthesis (local Hermes patch).

Extracted from ``gateway/run.py`` to shrink the merge surface of the
single largest local patch against upstream Hermes. Upstream's
``_kanban_notifier_watcher`` only sends the raw worker handoff; this
module layers an origin-profile LLM rewrite ("synthesize" notification
mode), a safe sanitized fallback for timeouts, and bounded artifact
excerpt loading so the synthesizer has the substantive answer even when
the worker summary is short.

Why a mixin: every entry point reaches GatewayRunner instance state
(``_conversation_locks``, ``_resolve_session_agent_runtime``,
``_run_in_executor_with_context``) and existing tests + callers reach
these methods via ``GatewayRunner`` (``runner._send_kanban_notification``
and ``GatewayRunner._build_kanban_synthesis_prompt`` as a staticmethod).
Inheriting via ``class GatewayRunner(KanbanSynthesisMixin)`` preserves
both call shapes without re-binding tricks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


class KanbanSynthesisMixin:
    """Synthesis + delivery helpers for Kanban completion notifications.

    All methods originally lived inline in ``GatewayRunner``. They depend
    on the following ``GatewayRunner`` attributes/methods, which are
    expected to be provided by the concrete class:

    * ``_conversation_lock_for_source(source) -> asyncio.Lock``
    * ``_resolve_session_agent_runtime(source=..., user_config=...) -> tuple``
    * ``_run_in_executor_with_context(fn)`` (awaitable)
    * ``_kanban_public_completion_fallback`` is defined here as a
      staticmethod, but the synthesizer also references it through
      ``self.`` (the test suite patches the bound name).
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
    ) -> None:
        """Send one Kanban notification and raise if the adapter reports failure.

        Platform adapters normally return ``SendResult`` instead of raising on
        delivery failure. The notifier must treat ``success=False`` as failure;
        otherwise it advances/unsubscribes and silently loses the completion
        ping even though nothing reached the user. ``notification_mode`` can
        opt into synthesized user-facing text or suppress delivery entirely.
        """
        mode = str(sub.get("notification_mode") or "direct").strip().lower()
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

        async def _send_and_mirror() -> None:
            nonlocal send_msg
            if mode == "synthesize" and getattr(event, "kind", None) == "completed":
                try:
                    synthesized = await self._synthesize_kanban_notification(
                        sub=sub,
                        event=event,
                        task=task,
                        board=board,
                        direct_message=msg,
                    )
                    if synthesized and str(synthesized).strip():
                        send_msg = str(synthesized).strip()
                except Exception as exc:
                    event_payload = getattr(event, "payload", None) or {}
                    worker_summary = event_payload.get("summary") or msg
                    send_msg = self._kanban_public_completion_fallback(
                        worker_summary=worker_summary,
                        worker_metadata=None,
                    )
                    logger.warning(
                        "kanban notifier: synthesis failed for %s; using sanitized public fallback: %s",
                        sub["task_id"], exc,
                    )
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

    @staticmethod
    def _build_kanban_synthesis_prompt(
        *,
        sub: dict,
        event: Any,
        task: Any,
        board: Optional[str],
        worker_summary: Any,
        worker_metadata: Any,
        artifact_context: str = "",
        direct_message: str,
    ) -> str:
        """Build the user-facing completion synthesis prompt."""
        origin_context = str(sub.get("origin_context") or "").strip()
        return (
            "You are the origin/default Hermes profile writing the final user-facing reply.\n"
            "Use only the provided handoff, metadata, task title/body, and origin context.\n"
            "Do NOT browse, fetch, run commands, create tasks, or redo data collection.\n"
            "Default UX rule: hide internal workflow plumbing. Do NOT mention Kanban, task ids, assignees, "
            "workers, worker/dispatcher flow, notification subscriptions, run ids, process ids, or board names "
            "unless the user explicitly asked for internal mechanics, debugging, task status, or audit details.\n"
            "Return the consolidated answer/result directly and concisely.\n"
            "If the handoff is insufficient, say what is missing and give the best available concise status; "
            "still avoid internal plumbing unless it is necessary to resolve the problem.\n\n"
            f"Original user/context excerpt:\n{origin_context[:2000]}\n\n"
            f"Task title/context:\n{getattr(task, 'title', '') if task else ''}\n"
            f"{str(getattr(task, 'body', '') or '')[:1200]}\n\n"
            f"Worker summary / durable handoff:\n{str(worker_summary or '')}\n\n"
            f"Worker metadata JSON:\n{json.dumps(worker_metadata, ensure_ascii=False, default=str)[:3000]}\n\n"
            f"Relevant artifact/report excerpts, if any:\n{str(artifact_context or '')[:12000]}\n\n"
            "Internal debug context (use only if the user's request is explicitly about internals/debugging):\n"
            f"Task id: {sub.get('task_id')}\n"
            f"Board: {board or 'default'}\n"
            f"Event kind: {getattr(event, 'kind', '')}\n"
            f"Run id: {getattr(event, 'run_id', '')}\n"
            f"Origin session id: {sub.get('origin_session_id') or ''}\n"
            f"Origin profile: {sub.get('origin_profile') or ''}\n"
            f"Direct fallback message: {direct_message}"
        )

    @staticmethod
    def _coerce_kanban_metadata(metadata: Any) -> Any:
        if isinstance(metadata, str):
            try:
                return json.loads(metadata)
            except Exception:
                return metadata
        return metadata

    @staticmethod
    def _read_kanban_artifact_context(
        *,
        task: Any,
        worker_metadata: Any,
        max_chars: int = 12000,
    ) -> str:
        """Return safe, small report/artifact excerpts for notification synthesis.

        Workers often write the substantive answer to a markdown artifact and
        keep the durable summary short ("created file at path"). The notifier
        runs without tools, so proactively include bounded excerpts from known
        kanban workspace report files. Restrict to text-like files under a
        kanban workspace and skip suspicious names to avoid leaking secrets.
        """
        metadata = KanbanSynthesisMixin._coerce_kanban_metadata(worker_metadata)
        if not isinstance(metadata, dict):
            return ""

        candidates: list[str] = []
        for key in (
            "artifact_path", "report_path", "source_file", "summary_path",
            "output_path", "markdown_path",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        for key in ("artifact_paths", "report_paths", "workspace_files_created"):
            value = metadata.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        candidates.append(item.strip())

        workspace_path = str(getattr(task, "workspace_path", "") or "").strip()
        resolved: list[Path] = []
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_absolute() and workspace_path:
                path = Path(workspace_path) / path
            resolved.append(path)

        allowed_root = Path(os.path.expanduser("~/.hermes/kanban/workspaces")).resolve()
        workspace_root = None
        if workspace_path:
            try:
                workspace_root = Path(workspace_path).expanduser().resolve()
            except Exception:
                workspace_root = None
        allowed_suffixes = {".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv"}
        secret_name_re = re.compile(r"(secret|token|credential|auth|cookie|key|\.env)", re.I)
        chunks: list[str] = []
        seen: set[str] = set()
        budget = max(0, int(max_chars))
        for path in resolved:
            if budget <= 0:
                break
            try:
                real = path.resolve()
                real_str = str(real)
                if real_str in seen:
                    continue
                seen.add(real_str)
                under_home_kanban = allowed_root in (real, *real.parents)
                under_task_workspace = bool(
                    workspace_root and workspace_root in (real, *real.parents)
                )
                parts = real.parts
                under_any_hermes_kanban = any(
                    parts[i:i + 3] == (".hermes", "kanban", "workspaces")
                    for i in range(0, max(0, len(parts) - 2))
                )
                if not (under_home_kanban or under_task_workspace or under_any_hermes_kanban):
                    continue
                if real.suffix.lower() not in allowed_suffixes:
                    continue
                if secret_name_re.search(real.name):
                    continue
                if not real.is_file() or real.stat().st_size > 200_000:
                    continue
                text = real.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            text = text.strip()
            if not text:
                continue
            excerpt = text[: min(len(text), budget)]
            if len(text) > len(excerpt):
                excerpt += "\n\n[artifact excerpt truncated]"
            chunks.append(f"Artifact excerpt ({real.name}):\n{excerpt}")
            budget -= len(excerpt)
        return "\n\n---\n\n".join(chunks)

    @staticmethod
    def _sanitize_kanban_public_text(text: Any, *, max_chars: int = 12000) -> str:
        """Remove internal Kanban plumbing from deterministic user-facing fallbacks."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"`/[^`\n]+`", "the generated report", cleaned)
        cleaned = re.sub(r"/Users/[^\s`，。]+", "the generated report", cleaned)
        cleaned = re.sub(r"\bt_[0-9a-f]{8,}\b", "the task", cleaned)
        cleaned = re.sub(r"@[A-Za-z0-9_-]+", "", cleaned)
        cleaned = re.sub(r"\bKanban\b", "", cleaned)
        cleaned = re.sub(r"\bworker(?:-[A-Za-z0-9_-]+)?\b", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\bdispatcher\b", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" —-\n")
        return cleaned[:max(0, int(max_chars))].strip()

    @staticmethod
    def _kanban_synthesis_timeout(user_config: Optional[dict]) -> float:
        """Return bounded Kanban completion synthesis timeout in seconds."""
        raw = ((user_config or {}).get("kanban") or {}).get(
            "synthesis_timeout_seconds", 120
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 120.0
        return max(5.0, min(value, 300.0))

    @staticmethod
    def _kanban_synthesis_route(user_config: Optional[dict]) -> dict:
        """Return sanitized route diagnostics for auxiliary Kanban synthesis."""
        aux = (user_config or {}).get("auxiliary") or {}
        route = aux.get("kanban_synthesis") if isinstance(aux, dict) else {}
        route = route if isinstance(route, dict) else {}
        return {
            "provider": str(route.get("provider") or "openrouter").strip() or "openrouter",
            "model": str(route.get("model") or "google/gemini-2.5-flash").strip() or "google/gemini-2.5-flash",
            "base_url": str(route.get("base_url") or "").strip(),
        }

    @staticmethod
    def _kanban_public_completion_fallback(
        *,
        worker_summary: Any,
        worker_metadata: Any,
        artifact_context: str = "",
    ) -> str:
        """Build a user-facing fallback when LLM synthesis times out/fails."""
        artifact_context = str(artifact_context or "").strip()
        if artifact_context:
            text = re.sub(r"^Artifact excerpt \([^)]*\):\n", "", artifact_context, count=1).strip()
            return KanbanSynthesisMixin._sanitize_kanban_public_text(text, max_chars=12000) or "Done."
        summary = KanbanSynthesisMixin._sanitize_kanban_public_text(worker_summary, max_chars=100000)
        if summary:
            return summary or "Done."
        metadata = KanbanSynthesisMixin._coerce_kanban_metadata(worker_metadata)
        if isinstance(metadata, dict):
            for key in ("recommendation", "result", "decision", "summary"):
                value = metadata.get(key)
                if value:
                    return KanbanSynthesisMixin._sanitize_kanban_public_text(value, max_chars=100000) or "Done."
        return "Done."

    async def _synthesize_kanban_notification(
        self,
        *,
        sub: dict,
        event: Any,
        task: Any,
        board: Optional[str],
        direct_message: str,
    ) -> str:
        """Run a lightweight no-tools origin-profile turn for a completion.

        The synthesis agent receives only the durable worker handoff and stored
        origin context. It intentionally has no toolsets, so it cannot re-run
        heavy collection or recursively create Kanban tasks/subscriptions.
        """
        from gateway.config import Platform as _Platform
        from gateway.session import SessionSource
        # Lazy import to avoid circular dependency at module load (gateway.run
        # imports this module). Safe at call time — run.py is fully loaded
        # before any notification gets synthesized.
        from gateway.run import _load_gateway_config

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
        user_config = _load_gateway_config()
        model, runtime_kwargs = self._resolve_session_agent_runtime(
            source=source,
            user_config=user_config,
        )

        event_payload = getattr(event, "payload", None) or {}
        run = None
        if getattr(event, "run_id", None):
            try:
                from hermes_cli import kanban_db as _kb
                conn = _kb.connect(board=board)
                try:
                    run = _kb.get_run(conn, int(event.run_id))
                finally:
                    conn.close()
            except Exception:
                run = None
        worker_summary = (
            (run.summary if run and run.summary else None)
            or event_payload.get("summary")
            or (task.result if task and getattr(task, "result", None) else None)
            or direct_message
        )
        worker_metadata = run.metadata if run and run.metadata is not None else None
        worker_metadata = self._coerce_kanban_metadata(worker_metadata)
        artifact_context = self._read_kanban_artifact_context(
            task=task,
            worker_metadata=worker_metadata,
        )
        prompt = self._build_kanban_synthesis_prompt(
            sub=sub,
            event=event,
            task=task,
            board=board,
            worker_summary=worker_summary,
            worker_metadata=worker_metadata,
            artifact_context=artifact_context,
            direct_message=direct_message,
        )

        fallback_reply = self._kanban_public_completion_fallback(
            worker_summary=worker_summary,
            worker_metadata=worker_metadata,
            artifact_context=artifact_context,
        )

        route = self._kanban_synthesis_route(user_config)
        timeout_s = self._kanban_synthesis_timeout(user_config)
        prompt_chars = len(prompt)
        context_chars = (
            len(str(sub.get("origin_context") or ""))
            + len(str(worker_summary or ""))
            + len(json.dumps(worker_metadata, ensure_ascii=False, default=str))
            + len(str(artifact_context or ""))
        )

        def run_sync() -> str:
            from agent.auxiliary_client import call_llm

            main_runtime = dict(runtime_kwargs or {})
            main_runtime["model"] = model
            response = call_llm(
                task="kanban_synthesis",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=900,
                timeout=timeout_s,
                main_runtime=main_runtime,
            )
            try:
                content = response.choices[0].message.content
            except Exception:
                content = ""
            return str(content or "").strip()

        started = time.monotonic()
        synthesis_status = "ok"
        synthesis_error = None
        try:
            return await asyncio.wait_for(
                self._run_in_executor_with_context(run_sync),
                timeout=timeout_s,
            )
        except Exception as exc:
            synthesis_status = "error"
            synthesis_error = str(exc) or type(exc).__name__
            elapsed = time.monotonic() - started
            is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            if fallback_reply:
                logger.warning(
                    "kanban notifier: synthesis failed; using sanitized public fallback "
                    "exc_type=%s timeout=%s elapsed=%.2fs provider=%s model=%s "
                    "prompt_chars=%d context_chars=%d task=%s",
                    type(exc).__name__,
                    is_timeout,
                    elapsed,
                    route.get("provider") or "auto",
                    route.get("model") or "default",
                    prompt_chars,
                    context_chars,
                    sub.get("task_id"),
                    exc_info=True,
                )
                return fallback_reply
            raise
        finally:
            try:
                from agent.telemetry import record_span_event

                record_span_event(
                    "kanban.synthesis",
                    platform="gateway",
                    profile=str(sub.get("origin_profile") or ""),
                    duration_ms=(time.monotonic() - started) * 1000,
                    status=synthesis_status,
                    error=synthesis_error,
                    attributes={
                        "platform": platform_str,
                        "notification_mode": str(sub.get("notification_mode") or "direct"),
                        "task_id": str(sub.get("task_id") or ""),
                        "request_id": str(sub.get("request_id") or ""),
                        "task_status": str(getattr(task, "status", "") or ""),
                    },
                )
            except Exception:
                pass


__all__ = ["KanbanSynthesisMixin"]
