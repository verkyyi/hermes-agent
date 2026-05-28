"""Kanban terminal-event notifier watcher (local Hermes patch).

Extracted from ``gateway/run.py`` to shrink the merge surface of the single
largest local patch against upstream Hermes — the same motivation and mixin
pattern as ``gateway/kanban_synthesis.KanbanSynthesisMixin`` (see
docs/LOCAL_PATCHES.md #1/#2). This module owns the background watcher that
polls ``kanban_notify_subs``, claims unseen terminal/heartbeat events, formats
the per-kind user-facing message, delegates delivery to
``_send_kanban_notification`` (the synthesis mixin), uploads any artifacts, and
advances / rewinds / clears the per-subscription delivery cursor.

Why a mixin: every method reaches ``GatewayRunner`` instance state and the
synthesis mixin, and existing tests reach them via ``GatewayRunner`` (e.g.
``runner._kanban_notifier_watcher(interval=1)``), so inheriting via
``class GatewayRunner(KanbanSynthesisMixin, KanbanNotifierMixin)`` preserves the
call shapes. The module-level progress/notify helpers and constants
(``_KANBAN_NOTIFY_KINDS``, ``_public_progress_interval_from_env``,
``_kanban_heartbeat_progress_message``) intentionally stay in ``gateway/run.py``
and are imported lazily inside the watcher to avoid a circular import
(``run.py`` imports this module for the class base).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


class KanbanNotifierMixin:
    """Background notifier watcher + cursor/artifact helpers for Kanban.

    Methods originally lived inline in ``GatewayRunner``. They depend on the
    following ``GatewayRunner`` attributes/methods, provided by the concrete
    class (or the synthesis mixin):

    * ``_running`` (bool) -- loop control flag
    * ``adapters`` (dict[Platform, BasePlatformAdapter]) -- connected adapters
    * ``_kanban_sub_fail_counts`` / ``_kanban_progress_sent_at`` -- per-sub
      transient state (lazily created here on the first tick)
    * ``_kanban_notifier_profile`` -- profile-ownership filter
    * ``_send_kanban_notification(...)`` -- from ``KanbanSynthesisMixin``
    """

    def _kanban_notify_in_gateway_enabled(self) -> bool:
        """Return whether this gateway should consume Kanban notify rows.

        Multiple profile gateways can be alive on the same host. Notification
        rows are a shared queue; whichever watcher consumes a row deletes it.
        A secondary profile with its own bot can therefore steal the default
        profile's subscription and fail delivery to an unknown chat.

        Default ownership follows ``kanban.dispatch_in_gateway`` because the
        dispatcher-owning gateway is the board owner in the current deployment
        model. Operators that run an external dispatcher but still want gateway
        notifications can explicitly set ``kanban.notify_in_gateway: true``.
        """
        env_override = os.environ.get("HERMES_KANBAN_NOTIFY_IN_GATEWAY", "").strip().lower()
        if env_override in ("0", "false", "no", "off"):
            return False
        if env_override in ("1", "true", "yes", "on"):
            return True
        try:
            from hermes_cli.config import load_config as _load_config
            cfg = _load_config()
        except Exception:
            return False
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if "notify_in_gateway" in kanban_cfg:
            return bool(kanban_cfg.get("notify_in_gateway"))
        return bool(kanban_cfg.get("dispatch_in_gateway", True))

    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return "default"

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. When a task reaches a terminal state
        (``completed`` / ``archived``), the subscription is removed.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Subscriptions live inside each board's own DB and cannot
        cross boards, so delivery semantics are unchanged — this is
        purely a fan-out of the single-DB poll.
        """
        from gateway.config import Platform as _Platform
        from gateway.run import (
            _KANBAN_TERMINAL_NOTIFY_KINDS,
            _KANBAN_NOTIFY_KINDS,
            _public_progress_interval_from_env,
            _kanban_heartbeat_progress_message,
        )
        if not self._kanban_notify_in_gateway_enabled():
            logger.info(
                "kanban notifier: disabled via config/env "
                "(kanban.notify_in_gateway=false or dispatch_in_gateway=false)"
            )
            return
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        TERMINAL_KINDS = _KANBAN_TERMINAL_NOTIFY_KINDS
        # Terminal event kinds trigger automatic unsubscription — the task
        # is done, blocked, or in a retry-needed state that the human
        # shouldn't keep pinging a stale chat for. Previously we only
        # unsubbed when task.status in ('done', 'archived'), which left
        # subscriptions on 'blocked' / 'gave_up' / 'crashed' / 'timed_out'
        # tasks stranded forever.
        TERMINAL_EVENT_KINDS = TERMINAL_KINDS
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        progress_sent_at: dict[tuple, int] = getattr(
            self, "_kanban_progress_sent_at", {}
        )
        self._kanban_progress_sent_at = progress_sent_at
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if owner_profile and owner_profile != notifier_profile:
                                    logger.debug(
                                        "kanban notifier: subscription for %s owned by profile %s; current profile %s skipping",
                                        sub.get("task_id"), owner_profile, notifier_profile,
                                    )
                                    continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=_KANBAN_NOTIFY_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "task": task,
                                    "board": slug,
                                })
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    adapter = self.adapters.get(plat)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    for ev in d["events"]:
                        kind = ev.kind
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        # Delivery mode is operator policy resolved from config
                        # per platform (not the per-task sub column). "public_mode"
                        # == synthesize: friendlier user-facing phrasing for
                        # non-completed events, and the wake owns completed events.
                        public_mode = self._resolve_kanban_notify_mode(
                            str(sub.get("platform") or "telegram").lower()
                        ) == "synthesize"
                        progress_msg = None
                        if kind == "heartbeat":
                            progress_interval = _public_progress_interval_from_env()
                            if progress_interval is None:
                                continue
                            sub_key_for_progress = (
                                sub["task_id"], sub["platform"],
                                sub["chat_id"], sub.get("thread_id") or "",
                            )
                            last_progress_at = progress_sent_at.get(sub_key_for_progress)
                            event_created_at = int(getattr(ev, "created_at", 0) or time.time())
                            if (
                                last_progress_at is not None
                                and event_created_at - last_progress_at < progress_interval
                            ):
                                continue
                            progress_msg = _kanban_heartbeat_progress_message(plat, ev)
                            if not progress_msg:
                                continue
                            msg = progress_msg
                        elif kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                handoff = f"\n{payload_summary.strip()}"
                            elif task and task.result:
                                handoff = f"\n{task.result.strip()}"
                            msg = (
                                f"✔ {tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = str(ev.payload["reason"])[:160]
                            if public_mode:
                                msg = (
                                    f"I need one clarification before I can continue: {reason}"
                                    if reason else
                                    "I need one clarification before I can continue."
                                )
                            else:
                                suffix = f": {reason}" if reason else ""
                                msg = f"⏸ {tag}Kanban {sub['task_id']} blocked{suffix}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            provider_public_message = ""
                            if ev.payload and ev.payload.get("public_message"):
                                provider_public_message = str(ev.payload["public_message"])[:500]
                            if public_mode:
                                msg = provider_public_message or (
                                    "I hit a backend issue and couldn’t finish this after retries. "
                                    "Ask for internal run details if you want me to inspect the failure."
                                )
                            else:
                                provider_suffix = ""
                                if ev.payload and ev.payload.get("provider_failure_kind"):
                                    provider_suffix = f" ({ev.payload['provider_failure_kind']})"
                                msg = (
                                    f"✖ {tag}Kanban {sub['task_id']} gave up{provider_suffix} "
                                    f"after repeated worker failures{err}"
                                )
                        elif kind == "crashed":
                            if public_mode:
                                msg = "I hit a backend issue while working on this. I’ll retry automatically."
                            else:
                                msg = (
                                    f"✖ {tag}Kanban {sub['task_id']} worker crashed "
                                    f"(pid gone); dispatcher will retry"
                                )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            if public_mode:
                                msg = "This is taking longer than expected, so I’m retrying it."
                            else:
                                msg = (
                                    f"⏱ {tag}Kanban {sub['task_id']} timed out "
                                    f"(max_runtime={limit}s); will retry"
                                )
                        else:
                            continue
                        metadata: dict[str, Any] = {}
                        if sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            await self._send_kanban_notification(
                                adapter, sub, msg, metadata,
                                event=ev, task=task, board=board_slug,
                            )
                            if kind == "heartbeat":
                                progress_sent_at[sub_key] = int(
                                    getattr(ev, "created_at", 0) or time.time()
                                )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries. Skipped in
                            # synthesize mode: the woken origin agent surfaces
                            # artifacts itself, so delivering here would
                            # double-upload.
                            if kind == "completed" and not public_mode:
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All events delivered; advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment on TERMINAL_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

