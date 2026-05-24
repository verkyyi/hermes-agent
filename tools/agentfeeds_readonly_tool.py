"""Read-only AgentFeeds tools for front-desk profiles.

These tools expose approved AgentFeeds state without granting terminal/file
access. They intentionally only read under the AgentFeeds cache root and never
subscribe, refresh, mutate secrets, execute commands, or expose arbitrary files.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from tools.registry import registry

TOOLSET = "agentfeeds_readonly"
DEFAULT_MAX_ITEMS = 20
MAX_ITEMS_HARD_LIMIT = 100
MAX_SEARCH_MATCHES = 50
MAX_TEXT_CHARS = 20_000

_LOCATION_STREAM_HINTS = (
    "location",
    "locate",
    "findmy",
    "find-my",
    "gps",
    "coordinate",
    "coordinates",
    "lat",
    "lon",
    "latitude",
    "longitude",
    "address",
    "whereami",
)

_EXACT_LOCATION_KEYS = {
    "lat",
    "lon",
    "lng",
    "latitude",
    "longitude",
    "coordinate",
    "coordinates",
    "gps",
    "position",
    "address",
    "formatted_address",
    "street",
    "street_address",
    "line1",
    "line2",
    "plus_code",
    "geohash",
    "map_url",
    "maps_url",
    "url",
}

_APPROX_LOCATION_KEYS = {
    "city",
    "region",
    "province",
    "state",
    "country",
    "country_code",
    "timezone",
    "time_zone",
    "observed_at",
    "timestamp",
    "time",
    "updated_at",
    "fetched_at",
    "generated_at",
    "source",
    "device",
    "name",
    "title",
}

_EXPLICIT_LOCATION_RE = re.compile(
    r"\b(exact|precise|coordinate|coordinates|lat(?:itude)?|lon(?:gitude)?|lng|address|where exactly|raw location)\b",
    re.IGNORECASE,
)


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _agentfeeds_root() -> Path:
    return Path(os.environ.get("AGENTFEEDS_ROOT", Path.home() / ".agentfeeds")).expanduser().resolve()


def _state_root() -> Path:
    return _agentfeeds_root() / "state"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not _is_relative_to(path, _state_root()):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _iter_state_files() -> Iterable[Path]:
    root = _state_root()
    if not root.exists():
        return []
    return sorted(path for path in root.glob("**/*.json") if path.is_file())


def _meta(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("_meta")
    return value if isinstance(value, dict) else {}


def _stream_id(payload: dict[str, Any], path: Path) -> str:
    meta = _meta(payload)
    stream_id = meta.get("subscription_id") or meta.get("id")
    if stream_id:
        return str(stream_id)
    rel = path.relative_to(_state_root()).with_suffix("")
    return str(rel).replace(os.sep, "/")


def _stream_summary(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    meta = _meta(payload)
    data = payload.get("data")
    item_count: int | None = None
    if isinstance(data, list):
        item_count = len(data)
    elif isinstance(data, dict):
        item_count = len(data)
    return {
        "id": _stream_id(payload, path),
        "title": meta.get("title"),
        "template_id": meta.get("template_id"),
        "stale": bool(meta.get("stale", False)),
        "status": meta.get("status") or ("stale" if meta.get("stale") else "ok"),
        "error": meta.get("error"),
        "updated_at": meta.get("updated_at") or meta.get("fetched_at") or meta.get("generated_at"),
        "item_count": item_count,
    }


def _load_streams() -> list[tuple[Path, dict[str, Any]]]:
    streams: list[tuple[Path, dict[str, Any]]] = []
    for path in _iter_state_files():
        payload = _read_json(path)
        if payload is not None:
            streams.append((path, payload))
    return streams


def _find_stream(stream_id: str) -> tuple[Path, dict[str, Any]] | None:
    wanted = (stream_id or "").strip().lower()
    if not wanted:
        return None
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path, payload in _load_streams():
        sid = _stream_id(payload, path)
        meta = _meta(payload)
        candidates = [sid, meta.get("title"), str(path.relative_to(_state_root()))]
        if any(str(candidate or "").lower() == wanted for candidate in candidates):
            return path, payload
        if any(wanted in str(candidate or "").lower() for candidate in candidates):
            matches.append((path, payload))
    if len(matches) == 1:
        return matches[0]
    return None


def _contains_location_hint(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if any(hint in key_l for hint in _LOCATION_STREAM_HINTS):
                return True
            if _contains_location_hint(item):
                return True
    elif isinstance(value, list):
        return any(_contains_location_hint(item) for item in value[:20])
    elif isinstance(value, str):
        text = value.lower()
        return any(hint in text for hint in _LOCATION_STREAM_HINTS)
    return False


def _is_location_stream(payload: dict[str, Any]) -> bool:
    meta = _meta(payload)
    haystack = " ".join(str(meta.get(k, "")) for k in ("subscription_id", "template_id", "title", "description"))
    if any(hint in haystack.lower() for hint in _LOCATION_STREAM_HINTS):
        return True
    return _contains_location_hint(payload.get("data"))


def _is_private_dm(platform: str | None, chat_id: str | None, user_id: str | None, args: dict[str, Any]) -> bool:
    if args.get("private_dm") is True:
        return True
    if args.get("private_dm") is False:
        return False
    if not platform:
        return False
    p = platform.lower()
    chat = str(chat_id or "")
    user = str(user_id or "")
    if p == "telegram" and chat and user and chat == user:
        return True
    if p in {"weixin", "wechat", "wecom"}:
        if chat.endswith("@chatroom") or chat.startswith("group"):
            return False
        return bool(chat and user and chat == user)
    return False


def _explicit_exact_location_requested(args: dict[str, Any], user_task: str | None) -> bool:
    if args.get("include_exact_location") is True:
        return True
    query = " ".join(str(x or "") for x in (args.get("query"), args.get("stream_id"), user_task))
    return bool(_EXPLICIT_LOCATION_RE.search(query))


def _may_return_exact_location(args: dict[str, Any], **kwargs: Any) -> bool:
    return _is_private_dm(
        kwargs.get("platform"), kwargs.get("chat_id"), kwargs.get("user_id"), args
    ) and _explicit_exact_location_requested(args, kwargs.get("user_task"))


def _sanitize_location(value: Any, allow_exact: bool) -> Any:
    if allow_exact:
        return value
    if isinstance(value, list):
        return [_sanitize_location(item, allow_exact) for item in value]
    if not isinstance(value, dict):
        return value
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        key_l = str(key).lower()
        if key_l in _EXACT_LOCATION_KEYS or any(hint == key_l for hint in _EXACT_LOCATION_KEYS):
            continue
        if isinstance(item, dict):
            nested = _sanitize_location(item, allow_exact)
            if nested not in ({}, [], None):
                sanitized[key] = nested
            continue
        if isinstance(item, list):
            sanitized[key] = _sanitize_location(item, allow_exact)
            continue
        if key_l in _APPROX_LOCATION_KEYS or key_l not in _EXACT_LOCATION_KEYS:
            sanitized[key] = item
    return sanitized


def _sanitize_payload(payload: dict[str, Any], args: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], bool]:
    if not _is_location_stream(payload):
        return payload, False
    allow_exact = _may_return_exact_location(args, **kwargs)
    if allow_exact:
        return payload, False
    safe = dict(payload)
    safe["data"] = _sanitize_location(payload.get("data"), allow_exact=False)
    safe["privacy_notice"] = (
        "Exact location fields were withheld. Exact coordinates/address require "
        "a private DM context and an explicit exact-location request."
    )
    return safe, True


def _truncate_value(value: Any, max_chars: int = MAX_TEXT_CHARS) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return value
    return {
        "truncated": True,
        "max_chars": max_chars,
        "preview": text[:max_chars],
    }


def _to_search_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _clamp_limit(value: Any, default: int = DEFAULT_MAX_ITEMS) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(MAX_ITEMS_HARD_LIMIT, n))


def _handle_agentfeeds_health(args: dict[str, Any], **kwargs: Any) -> str:
    streams = _load_streams()
    summaries = [_stream_summary(payload, path) for path, payload in streams]
    stale = [s for s in summaries if s.get("stale")]
    errors = [s for s in summaries if s.get("error") or str(s.get("status") or "").lower() == "error"]
    root = _agentfeeds_root()
    return _json_dumps({
        "ok": True,
        "root": str(root),
        "state_root_exists": _state_root().exists(),
        "stream_count": len(summaries),
        "stale_count": len(stale),
        "error_count": len(errors),
        "health": "ok" if not stale and not errors else "degraded",
        "stale_streams": stale[:20],
        "error_streams": errors[:20],
    })


def _handle_agentfeeds_streams_find(args: dict[str, Any], **kwargs: Any) -> str:
    query = str(args.get("query") or "").strip().lower()
    limit = _clamp_limit(args.get("limit"), default=50)
    results = []
    for path, payload in _load_streams():
        summary = _stream_summary(payload, path)
        haystack = " ".join(str(summary.get(k) or "") for k in ("id", "title", "template_id", "status")).lower()
        if not query or query in haystack:
            if _is_location_stream(payload):
                summary["privacy"] = "location_stream_exact_fields_guarded"
            results.append(summary)
    return _json_dumps({"ok": True, "query": query or None, "count": len(results), "streams": results[:limit]})


def _handle_agentfeeds_stream_read(args: dict[str, Any], **kwargs: Any) -> str:
    stream_id = str(args.get("stream_id") or "").strip()
    limit = _clamp_limit(args.get("limit"), default=DEFAULT_MAX_ITEMS)
    found = _find_stream(stream_id)
    if not found:
        return _json_dumps({"ok": False, "error": f"No active AgentFeeds stream matched '{stream_id}'"})
    path, payload = found
    payload, redacted = _sanitize_payload(payload, args, **kwargs)
    data = payload.get("data")
    if isinstance(data, list):
        payload = dict(payload)
        payload["data"] = data[:limit]
        payload["limit"] = limit
        payload["total_items"] = len(data)
    result = {
        "ok": True,
        "stream": _stream_summary(payload, path),
        "location_exact_redacted": redacted,
        "payload": _truncate_value(payload),
    }
    return _json_dumps(result)


def _handle_agentfeeds_search(args: dict[str, Any], **kwargs: Any) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return _json_dumps({"ok": False, "error": "query is required"})
    q = query.lower()
    limit = _clamp_limit(args.get("limit"), default=20)
    matches = []
    for path, payload in _load_streams():
        safe_payload, redacted = _sanitize_payload(payload, args, **kwargs)
        text = _to_search_text(safe_payload)
        if q not in text.lower():
            continue
        idx = text.lower().find(q)
        start = max(0, idx - 240)
        end = min(len(text), idx + len(q) + 480)
        matches.append({
            "stream": _stream_summary(safe_payload, path),
            "location_exact_redacted": redacted,
            "snippet": text[start:end],
        })
        if len(matches) >= MAX_SEARCH_MATCHES:
            break
    return _json_dumps({"ok": True, "query": query, "count": len(matches), "matches": matches[:limit]})


AGENTFEEDS_HEALTH_SCHEMA = {
    "name": "agentfeeds_health",
    "description": "Read-only AgentFeeds health summary: active state count, stale streams, and stream errors. Does not refresh or mutate feeds.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

AGENTFEEDS_STREAMS_FIND_SCHEMA = {
    "name": "agentfeeds_streams_find",
    "description": "Read-only list/find active AgentFeeds streams by id, title, template, or status.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional case-insensitive filter over stream id/title/template/status."},
            "limit": {"type": "integer", "description": "Maximum streams to return (1-100).", "default": 50},
        },
        "additionalProperties": False,
    },
}

AGENTFEEDS_STREAM_READ_SCHEMA = {
    "name": "agentfeeds_stream_read",
    "description": "Read one active AgentFeeds stream by id/title. Read-only; no refresh. Location streams redact exact coordinates/address unless private DM context and explicit exact-location request are both present.",
    "parameters": {
        "type": "object",
        "properties": {
            "stream_id": {"type": "string", "description": "Active stream id or exact/unique title substring, e.g. weather/santa-clara-current."},
            "limit": {"type": "integer", "description": "For list-like streams, maximum items to return (1-100).", "default": 20},
            "include_exact_location": {"type": "boolean", "description": "Set true only when the user explicitly asks for exact coordinates/address. The tool still requires private DM context."},
            "private_dm": {"type": "boolean", "description": "Optional explicit privacy context from the platform/session. Omit unless known."},
        },
        "required": ["stream_id"],
        "additionalProperties": False,
    },
}

AGENTFEEDS_SEARCH_SCHEMA = {
    "name": "agentfeeds_search",
    "description": "Search current AgentFeeds state across active streams. Read-only; returns snippets from cached state. Location exact fields are guarded like stream reads.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Case-insensitive text to search for in active stream state."},
            "limit": {"type": "integer", "description": "Maximum matches to return (1-100).", "default": 20},
            "include_exact_location": {"type": "boolean", "description": "Set true only when user explicitly asks for exact location. Requires private DM context."},
            "private_dm": {"type": "boolean", "description": "Optional explicit privacy context from the platform/session. Omit unless known."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def _check_agentfeeds_readonly_requirements() -> bool:
    # Tool availability should not depend on current stream health. A missing
    # state directory is a valid health result the tool can report.
    return True


registry.register(
    name="agentfeeds_health",
    toolset=TOOLSET,
    schema=AGENTFEEDS_HEALTH_SCHEMA,
    handler=_handle_agentfeeds_health,
    check_fn=_check_agentfeeds_readonly_requirements,
    emoji="📰",
    max_result_size_chars=60_000,
)
registry.register(
    name="agentfeeds_streams_find",
    toolset=TOOLSET,
    schema=AGENTFEEDS_STREAMS_FIND_SCHEMA,
    handler=_handle_agentfeeds_streams_find,
    check_fn=_check_agentfeeds_readonly_requirements,
    emoji="📰",
    max_result_size_chars=80_000,
)
registry.register(
    name="agentfeeds_stream_read",
    toolset=TOOLSET,
    schema=AGENTFEEDS_STREAM_READ_SCHEMA,
    handler=_handle_agentfeeds_stream_read,
    check_fn=_check_agentfeeds_readonly_requirements,
    emoji="📰",
    max_result_size_chars=80_000,
)
registry.register(
    name="agentfeeds_search",
    toolset=TOOLSET,
    schema=AGENTFEEDS_SEARCH_SCHEMA,
    handler=_handle_agentfeeds_search,
    check_fn=_check_agentfeeds_readonly_requirements,
    emoji="📰",
    max_result_size_chars=80_000,
)
