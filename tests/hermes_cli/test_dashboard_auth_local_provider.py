"""Tests for the bundled ``local`` shared-passcode dashboard-auth provider.

Two layers:

  1. Provider contract — protocol compliance, passcode check, self-signed
     token round-trip / expiry / tamper / passcode-rotation invalidation,
     and the register() activation-gating (skips with a reason when no
     passcode is configured).

  2. The ``/auth/password`` route flow — the small server-rendered passcode
     form added alongside the OAuth round trip for ``password_login``
     providers. Walked end-to-end through the real gate middleware + auth
     router in a self-contained app (no global ``web_server.app`` state).
"""
from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path

import pytest

from hermes_cli.dashboard_auth import (
    clear_providers,
    register_provider,
)
from hermes_cli.dashboard_auth.base import (
    InvalidCodeError,
    RefreshExpiredError,
    assert_protocol_compliance,
)
from hermes_cli.plugins import get_bundled_plugins_dir


# ---------------------------------------------------------------------------
# Load the plugin module by path (it isn't importable as a normal package).
# ---------------------------------------------------------------------------


def _load_local_plugin():
    path = get_bundled_plugins_dir() / "dashboard_auth" / "local" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "test_local_auth_plugin", path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_local_plugin()
LocalProvider = _mod.LocalPasswordDashboardAuthProvider


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


def test_local_complies_with_protocol():
    assert assert_protocol_compliance(LocalProvider) is None


def test_local_declares_password_login():
    assert LocalProvider.password_login is True


def test_start_login_redirects_to_password_page_with_state_cookie():
    p = LocalProvider(passcode="pw")
    ls = p.start_login(redirect_uri="http://192.168.1.79:9119/auth/callback")
    assert ls.redirect_url == "/auth/password"
    pkce = ls.cookie_payload["hermes_session_pkce"]
    assert pkce.startswith("state=") and len(pkce) > len("state=")


def test_complete_login_correct_passcode_returns_session():
    p = LocalProvider(passcode="hunter2", ttl_seconds=300)
    sess = p.complete_login(
        code="hunter2", state="x", code_verifier="", redirect_uri=""
    )
    assert sess.provider == "local"
    assert sess.user_id == "local"
    assert sess.refresh_token == ""  # no refresh tokens
    assert 290 <= sess.expires_at - int(time.time()) <= 300


def test_complete_login_wrong_passcode_raises():
    p = LocalProvider(passcode="hunter2")
    with pytest.raises(InvalidCodeError):
        p.complete_login(code="nope", state="x", code_verifier="", redirect_uri="")


def test_verify_session_round_trips():
    p = LocalProvider(passcode="pw", ttl_seconds=300)
    sess = p.complete_login(code="pw", state="x", code_verifier="", redirect_uri="")
    verified = p.verify_session(access_token=sess.access_token)
    assert verified is not None and verified.provider == "local"


def test_verify_expired_token_returns_none():
    p = LocalProvider(passcode="pw")
    # Forge a validly-signed but already-expired token.
    expired = p._mint_token(int(time.time()) - 1)
    assert p.verify_session(access_token=expired) is None


def test_verify_tampered_token_returns_none():
    p = LocalProvider(passcode="pw")
    sess = p.complete_login(code="pw", state="x", code_verifier="", redirect_uri="")
    assert p.verify_session(access_token=sess.access_token[:-2] + "xy") is None
    assert p.verify_session(access_token="garbage") is None
    assert p.verify_session(access_token="") is None


def test_rotating_passcode_invalidates_existing_token():
    p1 = LocalProvider(passcode="old")
    sess = p1.complete_login(code="old", state="x", code_verifier="", redirect_uri="")
    p2 = LocalProvider(passcode="new")  # different key derivation
    assert p2.verify_session(access_token=sess.access_token) is None


def test_refresh_always_raises():
    p = LocalProvider(passcode="pw")
    with pytest.raises(RefreshExpiredError):
        p.refresh_session(refresh_token="anything")


def test_revoke_is_silent():
    LocalProvider(passcode="pw").revoke_session(refresh_token="anything")


def test_construction_requires_passcode():
    with pytest.raises(ValueError):
        LocalProvider(passcode="")


# ---------------------------------------------------------------------------
# register() activation gating
# ---------------------------------------------------------------------------


class _CaptureCtx:
    def __init__(self):
        self.registered = []

    def register_dashboard_auth_provider(self, provider):
        self.registered.append(provider)


def test_register_skips_without_passcode(monkeypatch):
    # Isolate from the developer's real config.yaml.
    monkeypatch.setattr(_mod, "_load_config_local_auth_section", lambda: {})
    monkeypatch.delenv("HERMES_DASHBOARD_LOCAL_PASSCODE", raising=False)
    ctx = _CaptureCtx()
    _mod.register(ctx)
    assert ctx.registered == []
    assert "HERMES_DASHBOARD_LOCAL_PASSCODE" in _mod.LAST_SKIP_REASON


def test_register_activates_with_env_passcode(monkeypatch):
    monkeypatch.setattr(_mod, "_load_config_local_auth_section", lambda: {})
    monkeypatch.setenv("HERMES_DASHBOARD_LOCAL_PASSCODE", "from-env")
    ctx = _CaptureCtx()
    _mod.register(ctx)
    assert len(ctx.registered) == 1
    assert ctx.registered[0].name == "local"
    assert _mod.LAST_SKIP_REASON == ""


def test_register_reads_config_passcode_and_display_name(monkeypatch):
    monkeypatch.setattr(
        _mod,
        "_load_config_local_auth_section",
        lambda: {"passcode": "from-cfg", "display_name": "Home LAN"},
    )
    monkeypatch.delenv("HERMES_DASHBOARD_LOCAL_PASSCODE", raising=False)
    ctx = _CaptureCtx()
    _mod.register(ctx)
    assert len(ctx.registered) == 1
    assert ctx.registered[0].display_name == "Home LAN"


# ---------------------------------------------------------------------------
# /auth/password route flow (gate middleware + auth router, isolated app)
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """A minimal gated app wired with the local provider, like production."""
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse
    from fastapi.testclient import TestClient

    from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
    from hermes_cli.dashboard_auth.routes import router as auth_router

    clear_providers()
    register_provider(LocalProvider(passcode="s3cret", ttl_seconds=300))

    app = FastAPI()
    app.state.auth_required = True

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        return await gated_auth_middleware(request, call_next)

    app.include_router(auth_router)

    # A no-arg protected route: reaching it (200) proves the gate accepted
    # the session cookie. We deliberately avoid a ``request: Request`` param
    # here — under the editable-install import ordering, FastAPI can fail to
    # recognise it as the request object and 422s on a phantom query param.
    # Session *identity* is covered by the provider unit tests above.
    @app.get("/sessions")
    async def protected():
        return PlainTextResponse("ok")

    try:
        yield TestClient(
            app, base_url="http://192.168.1.79:9119", follow_redirects=False
        )
    finally:
        clear_providers()


def _walk_to_form(client):
    """Run /auth/login → /auth/password and return the form's CSRF state."""
    r = client.get("/auth/login", params={"provider": "local", "next": "/sessions"})
    assert r.status_code == 302 and r.headers["location"] == "/auth/password"
    r = client.get("/auth/password")
    assert r.status_code == 200
    return re.search(r'name="state" value="([^"]+)"', r.text).group(1)


def test_login_page_lists_local_provider(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "provider=local" in r.text
    assert "Local Password" in r.text


def test_unauthenticated_api_gets_401(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_password_form_renders_with_state(client):
    state = _walk_to_form(client)
    assert state


def test_correct_passcode_authenticates_and_lands_on_next(client):
    state = _walk_to_form(client)
    r = client.post("/auth/password", data={"state": state, "code": "s3cret"})
    assert r.status_code == 302 and r.headers["location"] == "/sessions"
    # Session cookie was set → the gate now lets the protected route through.
    r = client.get("/sessions")
    assert r.status_code == 200 and r.text == "ok"


def test_wrong_passcode_redirects_to_error(client):
    state = _walk_to_form(client)
    r = client.post("/auth/password", data={"state": state, "code": "WRONG"})
    assert r.status_code == 302 and "error=1" in r.headers["location"]
    # And the error page renders the message.
    r = client.get("/auth/password", params={"error": 1})
    assert "Incorrect passcode" in r.text


def test_csrf_state_mismatch_rejected(client):
    _walk_to_form(client)
    r = client.post("/auth/password", data={"state": "BADSTATE", "code": "s3cret"})
    assert r.status_code == 400


def test_password_page_without_flow_redirects_to_login(client):
    # No pkce cookie in flight → bounce back to /login rather than 500.
    r = client.get("/auth/password")
    assert r.status_code == 302 and r.headers["location"].endswith("/login")
