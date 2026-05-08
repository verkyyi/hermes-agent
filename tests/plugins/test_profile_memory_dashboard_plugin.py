"""Tests for the profile-memory dashboard plugin backend."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


PLUGIN_FILE = Path(__file__).resolve().parents[2] / "plugins" / "profile-memory" / "dashboard" / "plugin_api.py"
MANIFEST_FILE = Path(__file__).resolve().parents[2] / "plugins" / "profile-memory" / "dashboard" / "manifest.json"
BUNDLE_FILE = Path(__file__).resolve().parents[2] / "plugins" / "profile-memory" / "dashboard" / "dist" / "index.js"


def _load_plugin_module():
    assert PLUGIN_FILE.exists(), f"plugin file missing: {PLUGIN_FILE}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_profile_memory_test",
        PLUGIN_FILE,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "memories").mkdir()
    (home / "memories" / "USER.md").write_text("User likes concise answers.\n", encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text("Repo lives at /tmp/project.\n", encoding="utf-8")
    (home / "SOUL.md").write_text("You are Hermes.\n", encoding="utf-8")

    worker = home / "profiles" / "worker-code"
    (worker / "memories").mkdir(parents=True)
    (worker / "memories" / "USER.md").write_text("Worker user facts.\n", encoding="utf-8")
    (worker / "memories" / "MEMORY.md").write_text("Worker memory facts.\n", encoding="utf-8")
    (worker / "SOUL.md").write_text("Worker soul.\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def client(hermes_home):
    app = FastAPI()
    mod = _load_plugin_module()
    app.include_router(mod.router, prefix="/api/plugins/profile-memory")
    return TestClient(app)


def test_manifest_registers_memory_editor_tab():
    assert MANIFEST_FILE.exists()
    manifest = MANIFEST_FILE.read_text(encoding="utf-8")
    assert '"name": "profile-memory"' in manifest
    assert '"path": "/profile-memory"' in manifest
    assert '"api": "plugin_api.py"' in manifest
    assert BUNDLE_FILE.exists()
    bundle = BUNDLE_FILE.read_text(encoding="utf-8")
    assert 'window.__HERMES_PLUGINS__.register("profile-memory"' in bundle
    assert 'const API = "/api/plugins/profile-memory";' in bundle
    assert 'API + "/files?profile="' in bundle


def test_profiles_endpoint_lists_default_and_profile(client):
    r = client.get("/api/plugins/profile-memory/profiles")
    assert r.status_code == 200
    data = r.json()
    ids = [p["id"] for p in data["profiles"]]
    assert "default" in ids
    assert "worker-code" in ids
    assert all("path" not in p for p in data["profiles"])


def test_get_files_reads_whitelisted_memory_files(client):
    r = client.get("/api/plugins/profile-memory/files?profile=default")
    assert r.status_code == 200
    files = {f["key"]: f for f in r.json()["files"]}
    assert files["user"]["content"] == "User likes concise answers.\n"
    assert files["memory"]["content"] == "Repo lives at /tmp/project.\n"
    assert files["soul"]["content"] == "You are Hermes.\n"
    assert files["soul"]["editable"] is False
    assert all("/" not in f["relative_path"] or f["relative_path"] in {"memories/USER.md", "memories/MEMORY.md"} for f in files.values())


def test_put_file_updates_memory_and_creates_timestamped_backup(client, hermes_home):
    r = client.put(
        "/api/plugins/profile-memory/files/default/user",
        json={"content": "User prefers direct tradeoff analysis.\n"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["file"]["key"] == "user"
    assert data["file"]["content"] == "User prefers direct tradeoff analysis.\n"
    assert data["backup"]["created"] is True
    assert data["backup"]["relative_path"].startswith(".dashboard-memory-backups/")

    target = hermes_home / "memories" / "USER.md"
    assert target.read_text(encoding="utf-8") == "User prefers direct tradeoff analysis.\n"
    backups = list((hermes_home / ".dashboard-memory-backups").glob("USER.md.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "User likes concise answers.\n"


def test_put_memory_warns_but_does_not_echo_secret_in_logs_or_fields(client):
    r = client.put(
        "/api/plugins/profile-memory/files/default/memory",
        json={"content": "EXAMPLE_API_KEY=redacted-test-value\n"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["warnings"]
    assert any("secret" in w.lower() for w in data["warnings"])
    assert "redacted-test-value" in data["file"]["content"]
    assert "redacted-test-value" not in str(data["warnings"])


def test_rejects_unknown_profile_file_and_path_traversal(client):
    assert client.get("/api/plugins/profile-memory/files?profile=../default").status_code == 400
    assert client.get("/api/plugins/profile-memory/files?profile=missing").status_code == 404
    assert client.put(
        "/api/plugins/profile-memory/files/default/soul",
        json={"content": "attempted soul edit\n"},
    ).status_code == 403
    assert client.put(
        "/api/plugins/profile-memory/files/default/../../USER.md",
        json={"content": "bad\n"},
    ).status_code in {400, 404}


def test_backend_path_guard_blocks_symlink_escape(hermes_home):
    outside = hermes_home.parent / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    user_file = hermes_home / "memories" / "USER.md"
    user_file.unlink()
    user_file.symlink_to(outside)

    mod = _load_plugin_module()
    with pytest.raises(Exception):
        mod._profile_file("default", "user")
