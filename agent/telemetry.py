"""Local-first latency telemetry for Hermes turns.

MVP goals:
- fail open: telemetry must never affect agent responses;
- store turn + nested span timings in a local SQLite database;
- summarize perceived TTFA/TTFT/TTLT latencies for /metrics.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 10_000
_current_turn: contextvars.ContextVar["TurnRecorder | None"] = contextvars.ContextVar(
    "hermes_telemetry_turn", default=None
)
_store_singleton: "TelemetryStore | None" = None
_store_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return "{}"


def _safe_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (attrs or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[str(key)] = value
        else:
            try:
                out[str(key)] = json.loads(json.dumps(value, default=str))
            except Exception:
                out[str(key)] = str(value)
    return out


@dataclass
class SpanRecord:
    id: str
    turn_id: str
    parent_id: str | None
    name: str
    start_ms: int
    end_ms: int | None = None
    status: str = "ok"
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def finish(self, status: str = "ok", error: str | None = None, **attrs: Any) -> None:
        return None


class SpanHandle:
    def __init__(self, turn: "TurnRecorder", span: SpanRecord):
        self.turn = turn
        self.span = span
        self._closed = False

    def __enter__(self) -> "SpanHandle":
        self.turn._push_span(self.span.id)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        status = "error" if exc_type else "ok"
        err = str(exc)[:500] if exc else None
        self.finish(status=status, error=err)
        self.turn._pop_span(self.span.id)
        return False

    def finish(self, status: str = "ok", error: str | None = None, **attrs: Any) -> None:
        if self._closed:
            return
        self._closed = True
        self.turn.finish_span(self.span.id, status=status, error=error, **attrs)


class TurnRecorder:
    """In-memory recorder for one user-visible turn.

    Public methods catch their own errors so callers can use this directly in
    hot paths without wrapping every mark/span call.
    """

    def __init__(
        self,
        store: "TelemetryStore",
        *,
        session_id: str = "",
        platform: str = "unknown",
        profile: str = "",
        model: str = "",
        provider: str = "",
        attributes: dict[str, Any] | None = None,
        started_at_ms: int | None = None,
    ) -> None:
        self.store = store
        self.id = str(uuid.uuid4())
        self.session_id = session_id or ""
        self.platform = platform or "unknown"
        self.profile = profile or os.getenv("HERMES_PROFILE", "")
        self.model = model or ""
        self.provider = provider or ""
        self.attributes = _safe_attrs(attributes)
        self.started_at_ms = int(started_at_ms or _now_ms())
        self.ended_at_ms: int | None = None
        self.status = "ok"
        self.error: str | None = None
        self.first_ack_ms: int | None = None
        self.first_token_ms: int | None = None
        self.first_side_effect_ms: int | None = None
        self.output_start_ms: int | None = None
        self.output_end_ms: int | None = None
        self.output_chars = 0
        self.tool_count = 0
        self._spans: list[SpanRecord] = []
        self._span_stack: list[str] = []
        self._token: contextvars.Token | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._root = self.start_span("turn.total")
        self._root.__enter__()

    def set_current(self) -> None:
        try:
            self._token = _current_turn.set(self)
        except Exception:
            pass

    def clear_current(self) -> None:
        try:
            if self._token is not None:
                _current_turn.reset(self._token)
                self._token = None
        except Exception:
            pass

    def _elapsed_ms(self, when_ms: int | None = None) -> int:
        return max(0, int((when_ms or _now_ms()) - self.started_at_ms))

    def _push_span(self, span_id: str) -> None:
        with self._lock:
            self._span_stack.append(span_id)

    def _pop_span(self, span_id: str) -> None:
        with self._lock:
            if self._span_stack and self._span_stack[-1] == span_id:
                self._span_stack.pop()
            elif span_id in self._span_stack:
                self._span_stack.remove(span_id)

    def start_span(self, name: str, **attrs: Any) -> SpanHandle:
        try:
            with self._lock:
                parent_id = self._span_stack[-1] if self._span_stack else None
                span = SpanRecord(
                    id=str(uuid.uuid4()),
                    turn_id=self.id,
                    parent_id=parent_id,
                    name=str(name or "span"),
                    start_ms=_now_ms(),
                    attributes=_safe_attrs(attrs),
                )
                self._spans.append(span)
                return SpanHandle(self, span)
        except Exception:
            logger.debug("telemetry start_span failed", exc_info=True)
            return NullSpan()  # type: ignore[return-value]

    def finish_span(self, span_id: str, status: str = "ok", error: str | None = None, **attrs: Any) -> None:
        try:
            with self._lock:
                for span in reversed(self._spans):
                    if span.id == span_id:
                        span.end_ms = span.end_ms or _now_ms()
                        span.status = status or span.status
                        if error:
                            span.error = str(error)[:500]
                        if attrs:
                            span.attributes.update(_safe_attrs(attrs))
                        break
        except Exception:
            logger.debug("telemetry finish_span failed", exc_info=True)

    def mark_ack(self) -> None:
        try:
            with self._lock:
                self.first_ack_ms = self.first_ack_ms or _now_ms()
        except Exception:
            pass

    def mark_first_token(self) -> None:
        try:
            with self._lock:
                now = _now_ms()
                self.first_token_ms = self.first_token_ms or now
                self.output_start_ms = self.output_start_ms or now
                # TTFA falls back to first visible output when no explicit ack exists.
                self.first_ack_ms = self.first_ack_ms or now
        except Exception:
            pass

    def mark_output(self, text: str | None = None) -> None:
        try:
            with self._lock:
                now = _now_ms()
                self.first_token_ms = self.first_token_ms or now
                self.first_ack_ms = self.first_ack_ms or now
                self.output_start_ms = self.output_start_ms or now
                self.output_end_ms = now
                if text:
                    self.output_chars += len(text)
        except Exception:
            pass

    def mark_side_effect(self) -> None:
        try:
            with self._lock:
                self.first_side_effect_ms = self.first_side_effect_ms or _now_ms()
        except Exception:
            pass

    def increment_tool_count(self, n: int = 1) -> None:
        try:
            with self._lock:
                self.tool_count += max(0, int(n))
        except Exception:
            pass

    def update_attributes(self, **attrs: Any) -> None:
        try:
            with self._lock:
                self.attributes.update(_safe_attrs(attrs))
        except Exception:
            pass

    def finish(self, status: str = "ok", error: str | None = None, **attrs: Any) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with self._lock:
                self.ended_at_ms = self.ended_at_ms or _now_ms()
                self.status = status or self.status
                if error:
                    self.error = str(error)[:500]
                if attrs:
                    self.attributes.update(_safe_attrs(attrs))
                if self.output_start_ms and not self.output_end_ms:
                    self.output_end_ms = self.ended_at_ms
                for span in self._spans:
                    if span.end_ms is None:
                        span.end_ms = self.ended_at_ms
                self._root.finish(status=self.status, error=self.error)
                self.store.write_turn(self)
        except Exception:
            logger.debug("telemetry finish failed", exc_info=True)
        finally:
            self.clear_current()

    @property
    def turn_class(self) -> str:
        if self.platform == "cron":
            return "cron"
        if self.tool_count > 1:
            return "multi_hop"
        if self.tool_count == 1:
            return "tool_using"
        if self.output_chars and self.output_chars < 200:
            return "trivial"
        return "unknown"

    def to_turn_row(self) -> dict[str, Any]:
        ended = self.ended_at_ms or _now_ms()
        return {
            "id": self.id,
            "session_id": self.session_id,
            "started_at_ms": self.started_at_ms,
            "ended_at_ms": ended,
            "platform": self.platform,
            "profile": self.profile,
            "model": self.model,
            "provider": self.provider,
            "turn_class": self.turn_class,
            "status": self.status,
            "error": self.error,
            "ttfa_ms": self._elapsed_ms(self.first_ack_ms) if self.first_ack_ms else None,
            "ttft_ms": self._elapsed_ms(self.first_token_ms) if self.first_token_ms else None,
            "ttlt_ms": self._elapsed_ms(ended),
            "ttfs_ms": self._elapsed_ms(self.first_side_effect_ms) if self.first_side_effect_ms else None,
            "first_side_effect_ms": self.first_side_effect_ms,
            "output_stream_ms": (
                max(0, self.output_end_ms - self.output_start_ms)
                if self.output_start_ms and self.output_end_ms
                else None
            ),
            "output_chars": self.output_chars,
            "tool_count": self.tool_count,
            "attributes_json": _json_dumps(self.attributes),
        }

    def span_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for span in self._spans:
            rows.append({
                "id": span.id,
                "turn_id": span.turn_id,
                "parent_id": span.parent_id,
                "name": span.name,
                "start_ms": span.start_ms,
                "end_ms": span.end_ms or _now_ms(),
                "status": span.status,
                "error": span.error,
                "attributes_json": _json_dumps(span.attributes),
            })
        return rows


class TelemetryStore:
    def __init__(self, db_path: str | Path | None = None, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self.db_path = Path(db_path) if db_path else Path(get_hermes_home()) / "telemetry.db"
        self.max_turns = int(max_turns or DEFAULT_MAX_TURNS)
        self._lock = threading.RLock()
        self._disabled = False
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        except Exception:
            self._disabled = True
            logger.debug("telemetry disabled: init failed", exc_info=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=2.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    platform TEXT,
                    profile TEXT,
                    model TEXT,
                    provider TEXT,
                    turn_class TEXT,
                    status TEXT,
                    error TEXT,
                    ttfa_ms REAL,
                    ttft_ms REAL,
                    ttlt_ms REAL,
                    ttfs_ms REAL,
                    first_side_effect_ms INTEGER,
                    output_stream_ms REAL,
                    output_chars INTEGER DEFAULT 0,
                    tool_count INTEGER DEFAULT 0,
                    attributes_json TEXT
                );
                CREATE TABLE IF NOT EXISTS spans (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER,
                    status TEXT,
                    error TEXT,
                    attributes_json TEXT,
                    FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_turns_started ON turns(started_at_ms);
                CREATE INDEX IF NOT EXISTS idx_turns_group ON turns(platform, turn_class, started_at_ms);
                CREATE INDEX IF NOT EXISTS idx_spans_turn ON spans(turn_id, start_ms);
                """
            )

    def begin_turn(self, **kwargs: Any) -> TurnRecorder:
        if self._disabled:
            raise RuntimeError("telemetry store disabled")
        return TurnRecorder(self, **kwargs)

    def write_turn(self, turn: TurnRecorder) -> None:
        if self._disabled:
            return
        try:
            row = turn.to_turn_row()
            span_rows = turn.span_rows()
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO turns (
                        id, session_id, started_at_ms, ended_at_ms, platform, profile,
                        model, provider, turn_class, status, error, ttfa_ms, ttft_ms,
                        ttlt_ms, ttfs_ms, first_side_effect_ms, output_stream_ms,
                        output_chars, tool_count, attributes_json
                    ) VALUES (
                        :id, :session_id, :started_at_ms, :ended_at_ms, :platform, :profile,
                        :model, :provider, :turn_class, :status, :error, :ttfa_ms, :ttft_ms,
                        :ttlt_ms, :ttfs_ms, :first_side_effect_ms, :output_stream_ms,
                        :output_chars, :tool_count, :attributes_json
                    )
                    """,
                    row,
                )
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO spans (
                        id, turn_id, parent_id, name, start_ms, end_ms,
                        status, error, attributes_json
                    ) VALUES (
                        :id, :turn_id, :parent_id, :name, :start_ms, :end_ms,
                        :status, :error, :attributes_json
                    )
                    """,
                    span_rows,
                )
                self.prune(conn=conn)
        except Exception:
            logger.debug("telemetry write_turn failed", exc_info=True)

    def prune(self, conn: sqlite3.Connection | None = None) -> None:
        if self._disabled or self.max_turns <= 0:
            return
        def _do(c: sqlite3.Connection) -> None:
            c.execute(
                """
                DELETE FROM spans WHERE turn_id IN (
                    SELECT id FROM turns
                    ORDER BY started_at_ms DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_turns,),
            )
            c.execute(
                """
                DELETE FROM turns WHERE id IN (
                    SELECT id FROM turns
                    ORDER BY started_at_ms DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_turns,),
            )
        try:
            if conn is not None:
                _do(conn)
            else:
                with self._lock, self._connect() as c:
                    _do(c)
        except Exception:
            logger.debug("telemetry prune failed", exc_info=True)

    def query_turns(self, since_ms: int) -> list[dict[str, Any]]:
        if self._disabled:
            return []
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM turns
                    WHERE started_at_ms >= ?
                    ORDER BY started_at_ms DESC
                    """,
                    (since_ms,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.debug("telemetry query_turns failed", exc_info=True)
            return []

    def query_spans(self, since_ms: int) -> list[dict[str, Any]]:
        """Return span rows joined with low-cardinality turn dimensions."""
        if self._disabled:
            return []
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        s.id, s.turn_id, s.parent_id, s.name, s.start_ms, s.end_ms,
                        s.status, s.error, s.attributes_json,
                        t.platform, t.profile, t.turn_class, t.started_at_ms
                    FROM spans s
                    JOIN turns t ON t.id = s.turn_id
                    WHERE s.start_ms >= ?
                    ORDER BY s.start_ms DESC
                    """,
                    (since_ms,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.debug("telemetry query_spans failed", exc_info=True)
            return []

    def record_span_event(
        self,
        name: str,
        *,
        platform: str = "system",
        profile: str = "",
        status: str = "ok",
        error: str | None = None,
        attributes: dict[str, Any] | None = None,
        started_at_ms: int | None = None,
        ended_at_ms: int | None = None,
        duration_ms: float | int | None = None,
    ) -> None:
        """Persist a standalone telemetry span for background work.

        Gateway notifiers and the Kanban dispatcher often run outside an
        AIAgent turn, so ``current_turn()`` is absent. Represent those slices
        as a tiny synthetic turn containing ``turn.total`` plus the requested
        span. Callers should pass only low-cardinality, non-PII attributes.
        """
        if self._disabled:
            return
        try:
            end = int(ended_at_ms or _now_ms())
            if started_at_ms is not None:
                start = int(started_at_ms)
            elif duration_ms is not None:
                start = end - max(0, int(duration_ms))
            else:
                start = end
            if start > end:
                start = end
            turn_id = str(uuid.uuid4())
            root_id = str(uuid.uuid4())
            span_id = str(uuid.uuid4())
            attrs_json = _json_dumps(_safe_attrs(attributes))
            safe_error = str(error)[:500] if error else None
            turn_row = {
                "id": turn_id,
                "session_id": "",
                "started_at_ms": start,
                "ended_at_ms": end,
                "platform": platform or "system",
                "profile": profile or os.getenv("HERMES_PROFILE", ""),
                "model": "",
                "provider": "",
                "turn_class": "event",
                "status": status or "ok",
                "error": safe_error,
                "ttfa_ms": None,
                "ttft_ms": None,
                "ttlt_ms": max(0, end - start),
                "ttfs_ms": None,
                "first_side_effect_ms": None,
                "output_stream_ms": None,
                "output_chars": 0,
                "tool_count": 0,
                "attributes_json": attrs_json,
            }
            span_rows = [
                {
                    "id": root_id,
                    "turn_id": turn_id,
                    "parent_id": None,
                    "name": "turn.total",
                    "start_ms": start,
                    "end_ms": end,
                    "status": status or "ok",
                    "error": safe_error,
                    "attributes_json": attrs_json,
                },
                {
                    "id": span_id,
                    "turn_id": turn_id,
                    "parent_id": root_id,
                    "name": str(name or "span"),
                    "start_ms": start,
                    "end_ms": end,
                    "status": status or "ok",
                    "error": safe_error,
                    "attributes_json": attrs_json,
                },
            ]
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO turns (
                        id, session_id, started_at_ms, ended_at_ms, platform, profile,
                        model, provider, turn_class, status, error, ttfa_ms, ttft_ms,
                        ttlt_ms, ttfs_ms, first_side_effect_ms, output_stream_ms,
                        output_chars, tool_count, attributes_json
                    ) VALUES (
                        :id, :session_id, :started_at_ms, :ended_at_ms, :platform, :profile,
                        :model, :provider, :turn_class, :status, :error, :ttfa_ms, :ttft_ms,
                        :ttlt_ms, :ttfs_ms, :first_side_effect_ms, :output_stream_ms,
                        :output_chars, :tool_count, :attributes_json
                    )
                    """,
                    turn_row,
                )
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO spans (
                        id, turn_id, parent_id, name, start_ms, end_ms,
                        status, error, attributes_json
                    ) VALUES (
                        :id, :turn_id, :parent_id, :name, :start_ms, :end_ms,
                        :status, :error, :attributes_json
                    )
                    """,
                    span_rows,
                )
                self.prune(conn=conn)
        except Exception:
            logger.debug("telemetry record_span_event failed", exc_info=True)


class NullTurn:
    id = ""
    tool_count = 0

    def set_current(self) -> None: return None
    def clear_current(self) -> None: return None
    def start_span(self, *a: Any, **kw: Any) -> NullSpan: return NullSpan()
    def mark_ack(self) -> None: return None
    def mark_first_token(self) -> None: return None
    def mark_output(self, text: str | None = None) -> None: return None
    def mark_side_effect(self) -> None: return None
    def increment_tool_count(self, n: int = 1) -> None: return None
    def update_attributes(self, **attrs: Any) -> None: return None
    def finish(self, status: str = "ok", error: str | None = None, **attrs: Any) -> None: return None


def get_telemetry_store() -> TelemetryStore:
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                max_turns = int(os.getenv("HERMES_TELEMETRY_MAX_TURNS", str(DEFAULT_MAX_TURNS)) or DEFAULT_MAX_TURNS)
                _store_singleton = TelemetryStore(max_turns=max_turns)
    return _store_singleton


def current_turn() -> TurnRecorder | None:
    try:
        return _current_turn.get()
    except Exception:
        return None


@contextlib.contextmanager
def telemetry_span(name: str, **attrs: Any):
    turn = current_turn()
    if not turn:
        yield NullSpan()
        return
    with turn.start_span(name, **attrs) as span:
        yield span


def begin_turn_safe(**kwargs: Any) -> TurnRecorder | NullTurn:
    try:
        turn = get_telemetry_store().begin_turn(**kwargs)
        turn.set_current()
        return turn
    except Exception:
        logger.debug("begin_turn_safe failed", exc_info=True)
        return NullTurn()


def record_span_event(name: str, **kwargs: Any) -> None:
    """Best-effort standalone span recorder for non-agent background paths."""
    try:
        get_telemetry_store().record_span_event(name, **kwargs)
    except Exception:
        logger.debug("record_span_event failed", exc_info=True)


def _percentile(values: Iterable[float | int | None], p: float) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    rank = (len(vals) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def format_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1000:
        return f"{value:.0f}ms"
    return f"{value / 1000:.2f}s"


def _row_attrs(row: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = row.get("attributes_json")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _summarize_request_mix(rows: list[dict[str, Any]], span_rows: list[dict[str, Any]]) -> dict[str, Any]:
    foreground = [r for r in rows if (r.get("turn_class") or "") != "event"]
    total = len(foreground)
    kanban = 0
    direct = 0
    class_counts: dict[str, int] = {}
    platform_counts: dict[str, int] = {}
    notification_modes: dict[str, int] = {}
    for row in foreground:
        attrs = _row_attrs(row)
        platform = str(row.get("platform") or "unknown")
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        cls = str(row.get("turn_class") or "unknown")
        class_counts[cls] = class_counts.get(cls, 0) + 1
        req_class = str(attrs.get("request_class") or "direct")
        if req_class in {"async_kanban", "kanban_dispatched"} or attrs.get("kanban_task_id"):
            kanban += 1
            mode = str(attrs.get("notification_mode") or "unknown")
            notification_modes[mode] = notification_modes.get(mode, 0) + 1
        else:
            direct += 1
    # Async completions may arrive in standalone gateway spans; include their
    # notification modes even when the foreground turn fell outside the window.
    for row in span_rows:
        attrs = _row_attrs(row)
        if attrs.get("notification_mode") and (attrs.get("request_id") or attrs.get("task_id")):
            mode = str(attrs.get("notification_mode") or "unknown")
            notification_modes[mode] = notification_modes.get(mode, 0) + 1
    return {
        "total_foreground": total,
        "direct_no_kanban": direct,
        "kanban_dispatched": kanban,
        "direct_pct": (direct / total * 100.0) if total else 0.0,
        "kanban_pct": (kanban / total * 100.0) if total else 0.0,
        "turn_classes": class_counts,
        "platforms": platform_counts,
        "notification_modes": notification_modes,
    }


def _summarize_async_kanban(rows: list[dict[str, Any]], span_rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests: dict[str, dict[str, Any]] = {}
    tasks_to_request: dict[str, str] = {}
    for row in rows:
        attrs = _row_attrs(row)
        request_id = str(attrs.get("request_id") or row.get("id") or "").strip()
        if not request_id:
            continue
        entry = requests.setdefault(request_id, {"request_id": request_id, "stages": {}})
        if (row.get("turn_class") or "") != "event":
            entry.setdefault("received_at_ms", row.get("started_at_ms"))
            if row.get("ttfa_ms") is not None and row.get("started_at_ms") is not None:
                entry.setdefault("first_visible_ack_ms", row.get("started_at_ms") + row.get("ttfa_ms"))
            if attrs.get("kanban_task_id"):
                task_id = str(attrs.get("kanban_task_id"))
                entry.setdefault("task_id", task_id)
                tasks_to_request[task_id] = request_id
            if attrs.get("notification_mode"):
                entry.setdefault("notification_mode", attrs.get("notification_mode"))
    for row in span_rows:
        attrs = _row_attrs(row)
        request_id = str(attrs.get("request_id") or "").strip()
        task_id = str(attrs.get("task_id") or "").strip()
        if not request_id and task_id in tasks_to_request:
            request_id = tasks_to_request[task_id]
        if not request_id:
            continue
        entry = requests.setdefault(request_id, {"request_id": request_id, "stages": {}})
        if task_id:
            entry.setdefault("task_id", task_id)
            tasks_to_request[task_id] = request_id
        if attrs.get("notification_mode"):
            entry.setdefault("notification_mode", attrs.get("notification_mode"))
        name = str(row.get("name") or "")
        start = row.get("start_ms")
        end = row.get("end_ms")
        if name and start is not None and end is not None:
            try:
                entry["stages"].setdefault(name, []).append(max(0.0, float(end) - float(start)))
            except Exception:
                pass
        if name == "kanban.task_created" and start is not None:
            entry.setdefault("task_created_ms", start)
        elif name == "kanban.dispatch_ack_sent" and start is not None:
            entry.setdefault("dispatch_ack_sent_ms", start)
        elif name in {"kanban.final_notification_sent", "gateway.send"} and end is not None:
            entry["final_notification_sent_ms"] = max(entry.get("final_notification_sent_ms") or 0, end)
    completed = []
    ttfa_async = []
    ttlt_async = []
    stage_durations: dict[str, list[float]] = {}
    for entry in requests.values():
        is_async_kanban = bool(
            entry.get("task_id")
            or entry.get("task_created_ms")
            or entry.get("dispatch_ack_sent_ms")
            or entry.get("final_notification_sent_ms")
            or entry.get("stages")
        )
        if not is_async_kanban:
            continue
        if entry.get("task_id"):
            completed.append(entry)
        received = entry.get("received_at_ms")
        ack = entry.get("dispatch_ack_sent_ms") or entry.get("first_visible_ack_ms") or entry.get("task_created_ms")
        final = entry.get("final_notification_sent_ms")
        if received is not None and ack is not None:
            try:
                ttfa_async.append(max(0.0, float(ack) - float(received)))
            except Exception:
                pass
        if received is not None and final is not None:
            try:
                ttlt_async.append(max(0.0, float(final) - float(received)))
            except Exception:
                pass
        for name, vals in entry.get("stages", {}).items():
            if name == "turn.total":
                continue
            stage_durations.setdefault(name, []).extend(vals)
    stage_summary = []
    for name, vals in sorted(stage_durations.items(), key=lambda kv: sum(kv[1]), reverse=True)[:12]:
        stage_summary.append({
            "name": name,
            "count": len(vals),
            "p50_ms": _percentile(vals, 0.50),
            "p95_ms": _percentile(vals, 0.95),
        })
    return {
        "requests": len(completed),
        "completed_notifications": sum(1 for e in completed if e.get("final_notification_sent_ms")),
        "ttfa_async_p50_ms": _percentile(ttfa_async, 0.50),
        "ttfa_async_p95_ms": _percentile(ttfa_async, 0.95),
        "ttlt_async_p50_ms": _percentile(ttlt_async, 0.50),
        "ttlt_async_p95_ms": _percentile(ttlt_async, 0.95),
        "stages": stage_summary,
    }


def summarize_metrics(window_hours: float = 24.0, store: TelemetryStore | None = None) -> dict[str, Any]:
    store = store or get_telemetry_store()
    since_ms = _now_ms() - int(float(window_hours) * 3600 * 1000)
    rows = store.query_turns(since_ms)
    span_rows = store.query_spans(since_ms)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("platform") or "unknown", row.get("turn_class") or "unknown")
        groups.setdefault(key, []).append(row)

    summaries = []
    for (platform, turn_class), items in sorted(groups.items()):
        summaries.append({
            "platform": platform,
            "turn_class": turn_class,
            "count": len(items),
            "ttfa_p50_ms": _percentile((r.get("ttfa_ms") for r in items), 0.50),
            "ttfa_p95_ms": _percentile((r.get("ttfa_ms") for r in items), 0.95),
            "ttft_p50_ms": _percentile((r.get("ttft_ms") for r in items), 0.50),
            "ttft_p95_ms": _percentile((r.get("ttft_ms") for r in items), 0.95),
            "ttlt_p50_ms": _percentile((r.get("ttlt_ms") for r in items), 0.50),
            "ttlt_p95_ms": _percentile((r.get("ttlt_ms") for r in items), 0.95),
            "stream_p50_ms": _percentile((r.get("output_stream_ms") for r in items), 0.50),
            "stream_p95_ms": _percentile((r.get("output_stream_ms") for r in items), 0.95),
            "tools_p50": _percentile((r.get("tool_count") for r in items), 0.50),
        })
    span_groups: dict[str, list[float]] = {}
    for row in span_rows:
        name = row.get("name") or "span"
        if name == "turn.total":
            continue
        start = row.get("start_ms")
        end = row.get("end_ms")
        if start is None or end is None:
            continue
        try:
            duration = max(0.0, float(end) - float(start))
        except (TypeError, ValueError):
            continue
        span_groups.setdefault(str(name), []).append(duration)
    span_summaries = []
    for name, durations in sorted(
        span_groups.items(), key=lambda item: sum(item[1]), reverse=True,
    )[:12]:
        span_summaries.append({
            "name": name,
            "count": len(durations),
            "total_ms": sum(durations),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
        })
    return {
        "window_hours": window_hours,
        "total_turns": len(rows),
        "groups": summaries,
        "spans": span_summaries,
        "request_mix": _summarize_request_mix(rows, span_rows),
        "async_kanban": _summarize_async_kanban(rows, span_rows),
    }


def format_metrics_summary(window_hours: float = 24.0, store: TelemetryStore | None = None) -> str:
    """Render the metrics summary as vertical, mobile-friendly cards.

    Output uses standard markdown (``**bold**`` / ``*italic*``) rather than
    space-padded columns: Telegram renders message text in a proportional
    font, so fixed-width tables never line up. Each platform/class becomes a
    short labeled block that reads cleanly on a phone with no sideways scroll.
    Latency values are shown as ``p50 / p95``.
    """
    def _pair(p50: float | None, p95: float | None) -> str:
        return f"{format_ms(p50)} / {format_ms(p95)}"

    summary = summarize_metrics(window_hours=window_hours, store=store)
    hours = summary["window_hours"]
    total = summary["total_turns"]
    lines = [f"**Hermes metrics — last {hours:g}h**", f"*{total} turns*"]
    if not summary["groups"]:
        lines.append("")
        lines.append("No telemetry recorded yet.")
        return "\n".join(lines)
    for g in summary["groups"]:
        lines.append("")
        lines.append(f"**{g['platform']}/{g['turn_class']}** · {g['count']} turns")
        lines.append(f"  TTFA   {_pair(g['ttfa_p50_ms'], g['ttfa_p95_ms'])}")
        lines.append(f"  TTFT   {_pair(g['ttft_p50_ms'], g['ttft_p95_ms'])}")
        lines.append(f"  TTLT   {_pair(g['ttlt_p50_ms'], g['ttlt_p95_ms'])}")
        lines.append(f"  stream {_pair(g['stream_p50_ms'], g['stream_p95_ms'])}")
    mix = summary.get("request_mix") or {}
    if mix:
        lines.append("")
        lines.append("**Request mix**")
        lines.append(f"  foreground: {mix.get('total_foreground', 0)}")
        lines.append(
            f"  direct/no-kanban: {mix.get('direct_no_kanban', 0)} "
            f"({mix.get('direct_pct', 0):.0f}%)"
        )
        lines.append(
            f"  kanban-dispatched: {mix.get('kanban_dispatched', 0)} "
            f"({mix.get('kanban_pct', 0):.0f}%)"
        )
        modes = mix.get("notification_modes") or {}
        if modes:
            parts = ", ".join(f"{k}:{v}" for k, v in sorted(modes.items()))
            lines.append(f"  notification modes: {parts}")
    async_summary = summary.get("async_kanban") or {}
    if async_summary and async_summary.get("requests"):
        lines.append("")
        lines.append("**Async kanban**")
        lines.append(f"  requests: {async_summary.get('requests', 0)}")
        lines.append(
            f"  completed notifications: {async_summary.get('completed_notifications', 0)}"
        )
        lines.append(
            f"  TTFA_async {_pair(async_summary.get('ttfa_async_p50_ms'), async_summary.get('ttfa_async_p95_ms'))}"
        )
        lines.append(
            f"  TTLT_async {_pair(async_summary.get('ttlt_async_p50_ms'), async_summary.get('ttlt_async_p95_ms'))}"
        )
    if summary.get("spans"):
        lines.append("")
        lines.append("**Spans** (top by total time)")
        for sp in summary["spans"]:
            lines.append(
                f"  {sp['name']} · {sp['count']}× · total {format_ms(sp['total_ms'])} · "
                f"{_pair(sp['p50_ms'], sp['p95_ms'])}"
            )
    lines.append("")
    lines.append("*Values shown as p50 / p95.*")
    lines.append(
        "*TTFA=first ack/status or visible output · TTFT=first visible token · "
        "TTLT=final response. Async TTLT correlates the foreground request_id "
        "to the Kanban completion notification.*"
    )
    return "\n".join(lines)
