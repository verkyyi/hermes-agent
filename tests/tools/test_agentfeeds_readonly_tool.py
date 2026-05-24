import json
from pathlib import Path

from model_tools import get_tool_definitions, handle_function_call
from toolsets import resolve_toolset, validate_toolset


def _write_state(root: Path, rel: str, payload: dict) -> None:
    path = root / "state" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_agentfeeds_readonly_toolset_definitions_available():
    assert validate_toolset("agentfeeds_readonly") is True
    assert set(resolve_toolset("agentfeeds_readonly")) == {
        "agentfeeds_health",
        "agentfeeds_streams_find",
        "agentfeeds_stream_read",
        "agentfeeds_search",
    }

    schemas = get_tool_definitions(enabled_toolsets=["agentfeeds_readonly"], quiet_mode=True)
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {
        "agentfeeds_health",
        "agentfeeds_streams_find",
        "agentfeeds_stream_read",
        "agentfeeds_search",
    }


def test_agentfeeds_stream_read_redacts_exact_location_without_private_explicit_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFEEDS_ROOT", str(tmp_path))
    _write_state(
        tmp_path,
        "personal/verky-location.json",
        {
            "_meta": {
                "subscription_id": "personal/verky-location",
                "template_id": "personal/location",
                "title": "Verky latest location",
                "stale": False,
            },
            "data": {
                "observed_at": "2026-05-13T18:35:45+08:00",
                "latitude": 30.9346212,
                "longitude": 114.4537232,
                "address": "exact private address",
                "city": "Wuhan",
                "region": "Hubei",
            },
        },
    )

    result = json.loads(handle_function_call(
        "agentfeeds_stream_read",
        {"stream_id": "personal/verky-location", "include_exact_location": True},
        platform="telegram",
        chat_id="group-1",
        user_id="123",
    ))

    assert result["ok"] is True
    assert result["location_exact_redacted"] is True
    data = result["payload"]["data"]
    assert data["city"] == "Wuhan"
    assert data["region"] == "Hubei"
    assert data["observed_at"] == "2026-05-13T18:35:45+08:00"
    assert "latitude" not in data
    assert "longitude" not in data
    assert "address" not in data


def test_agentfeeds_stream_read_allows_exact_location_only_for_private_dm_and_explicit_request(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFEEDS_ROOT", str(tmp_path))
    _write_state(
        tmp_path,
        "personal/verky-location.json",
        {
            "_meta": {
                "subscription_id": "personal/verky-location",
                "template_id": "personal/location",
                "title": "Verky latest location",
            },
            "data": {"latitude": 1.23, "longitude": 4.56, "city": "Wuhan"},
        },
    )

    result = json.loads(handle_function_call(
        "agentfeeds_stream_read",
        {"stream_id": "personal/verky-location", "include_exact_location": True},
        platform="telegram",
        chat_id="123",
        user_id="123",
    ))

    assert result["ok"] is True
    assert result["location_exact_redacted"] is False
    assert result["payload"]["data"]["latitude"] == 1.23
    assert result["payload"]["data"]["longitude"] == 4.56


def test_agentfeeds_search_is_readonly_over_cached_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFEEDS_ROOT", str(tmp_path))
    _write_state(
        tmp_path,
        "weather/current.json",
        {
            "_meta": {
                "subscription_id": "weather/santa-clara-current",
                "template_id": "weather/openmeteo-current",
                "title": "Santa Clara current weather",
                "stale": False,
            },
            "data": {"temperature_c": 21.5, "conditions_code": 1},
        },
    )

    health = json.loads(handle_function_call("agentfeeds_health", {}))
    assert health["stream_count"] == 1
    assert health["health"] == "ok"

    search = json.loads(handle_function_call("agentfeeds_search", {"query": "Santa Clara"}))
    assert search["ok"] is True
    assert search["count"] == 1
    assert search["matches"][0]["stream"]["id"] == "weather/santa-clara-current"
