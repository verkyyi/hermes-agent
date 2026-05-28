"""Fork-local ``GatewayRunner`` methods (local Hermes patch).

Extracted from ``gateway/run.py`` to shrink the merge surface of the single
largest local patch against upstream Hermes — the same motivation and mixin
pattern as ``gateway/kanban_synthesis.KanbanSynthesisMixin`` and
``gateway/kanban_notifier.KanbanNotifierMixin`` (see docs/LOCAL_PATCHES.md).

This module owns ``GatewayRunner`` methods that are *entirely* fork-added (they
have no upstream counterpart), so lifting them out byte-for-byte removes their
lines from the upstream-owned region of ``gateway/run.py`` without changing any
behavior:

* ``_conversation_lock_for_key`` / ``_conversation_lock_for_source`` — the
  per-session conversation serialization locks introduced for the Kanban
  completion-synthesis mirroring patch.
* ``_handle_metrics_command`` — the ``/metrics`` slash-command handler that
  surfaces local latency telemetry.

Why a mixin: every method reaches ``GatewayRunner`` instance state/methods
(``self._conversation_locks``, ``self._session_key_for_source``), and existing
tests reach them via a ``GatewayRunner`` instance (e.g.
``runner._conversation_lock_for_source(source)``), so inheriting via
``class GatewayRunner(..., ForkLocalGatewayMixin)`` preserves the call shapes.

Module-level fork-added helpers/constants in ``gateway/run.py`` (the pre-LLM-ack
and public-progress heuristics, the timestamp-prefix and telemetry helpers, the
Kanban notify-kind constants) intentionally stay in ``gateway/run.py`` because
tests import them as ``from gateway.run import ...`` and the notifier mixin
imports a few of them lazily; moving them would break those imports for no
surface gain.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gateway.config import MessageEvent, SessionSource


class ForkLocalGatewayMixin:
    """Whole fork-added ``GatewayRunner`` methods (no upstream counterpart).

    Depends on the following ``GatewayRunner`` attributes/methods, provided by
    the concrete class:

    * ``_conversation_locks`` (dict[str, asyncio.Lock]) -- per-session locks
    * ``_session_key_for_source(source)`` -- session-key resolver
    """

    def _conversation_lock_for_key(self, session_key: str) -> asyncio.Lock:
        """Return the per-session conversation serialization lock.

        Native user turns, queued follow-ups, and synthetic completion replies
        all acquire this lock before mutating session history or mirroring an
        assistant message. Locks are per session key, so unrelated chats keep
        running concurrently.
        """
        locks = getattr(self, "_conversation_locks", None)
        if locks is None:
            locks = {}
            self._conversation_locks = locks
        lock = locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            locks[session_key] = lock
        return lock

    def _conversation_lock_for_source(self, source: "SessionSource") -> asyncio.Lock:
        return self._conversation_lock_for_key(self._session_key_for_source(source))

    async def _handle_metrics_command(self, event: "MessageEvent") -> str:
        """Handle /metrics command -- concise local latency telemetry."""
        hours = 24.0
        args = event.get_command_args().strip()
        if args:
            try:
                hours = max(0.1, float(args.split()[0]))
            except Exception:
                return "Usage: /metrics [hours]"
        try:
            from agent.telemetry import format_metrics_summary
            return format_metrics_summary(window_hours=hours)
        except Exception as exc:
            return f"Telemetry metrics unavailable: {exc}"
