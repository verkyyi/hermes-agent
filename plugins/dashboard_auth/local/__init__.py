"""LocalPasswordDashboardAuthProvider — a self-contained shared-passcode gate.

A ``DashboardAuthProvider`` for trusted-LAN binds where the Nous Portal OAuth
provider isn't available (no ``agent:{instance_id}`` client_id provisioned).
Instead of bouncing through an external IDP, the operator configures a single
shared passcode; anyone who enters it gets a session.

Why this exists
---------------
Binding the dashboard to a non-loopback host (``--host 0.0.0.0``) engages the
auth gate, which fails closed unless a provider is registered. The only bundled
provider (``nous``) needs a Portal-issued client_id. This plugin provides an
authenticated alternative that needs nothing but a passcode — so the dashboard
can be exposed on a home LAN without either ``--insecure`` (no auth at all) or
a Portal account.

Configuration surfaces (env wins over config.yaml when set non-empty):

  ``config.yaml`` — canonical surface::

      dashboard:
        local_auth:
          passcode: "your-shared-secret"   # required to activate
          display_name: "Local Password"   # optional, shown on /login
          ttl_seconds: 43200               # optional, session lifetime (12h)

  Environment overrides::

      HERMES_DASHBOARD_LOCAL_PASSCODE       — the shared secret
      HERMES_DASHBOARD_LOCAL_DISPLAY_NAME   — optional login-page label
      HERMES_DASHBOARD_LOCAL_TTL_SECONDS    — optional session lifetime

Session model
-------------
There is no external IDP, so this provider mints and verifies its own session
tokens. A token is ``b64url(exp) . b64url(HMAC-SHA256(key, exp))`` where ``key``
is derived deterministically from the passcode. Consequences:

  - Tokens are stateless and self-validating — no server-side session store, so
    sessions survive a dashboard restart.
  - Rotating the passcode changes ``key`` and therefore invalidates every live
    session (old signatures no longer verify) — a free "log everyone out".
  - There are no refresh tokens; when the access token expires the middleware
    redirects to ``/login`` and the user re-enters the passcode. Re-auth is
    cheap, so this is fine.

How the passcode is collected
-----------------------------
The provider declares ``password_login = True``. ``start_login`` doesn't build
an external authorize URL — it returns a relative redirect to the host
application's ``/auth/password`` page (a small server-rendered passcode form
added alongside the OAuth routes) and stashes a CSRF ``state`` nonce in the
pkce cookie exactly like the OAuth providers do. The form POSTs the passcode
back; the route validates the CSRF nonce and calls ``complete_login`` with the
passcode as the ``code``.

Skip reasons
------------
Mirrors the nous provider: when the plugin loads but no passcode is configured,
it writes a human-readable reason to module-level ``LAST_SKIP_REASON`` so the
gate's fail-closed message can be specific.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCodeError,
    LoginStart,
    RefreshExpiredError,
    Session,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_DISPLAY_NAME = "Local Password"
_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12 hours
_MIN_TTL_SECONDS = 60
# Fixed user identity — this is a single shared account, not per-user.
_LOCAL_USER_ID = "local"
# Domain-separation tag mixed into the key derivation so the signing key can
# never collide with the passcode used for some other purpose.
_KEY_CONTEXT = b"hermes-dashboard-local-auth/v1"
# Where the passcode form posts to; owned by the host application's auth
# routes (allowlisted pre-auth alongside /auth/login & /auth/callback).
_PASSWORD_PAGE_PATH = "/auth/password"


# ---------------------------------------------------------------------------
# Skip-reason channel (see nous provider for rationale)
# ---------------------------------------------------------------------------

LAST_SKIP_REASON: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LocalPasswordDashboardAuthProvider(DashboardAuthProvider):
    """Shared-passcode gate with self-signed HMAC session tokens."""

    name = "local"
    display_name = _DEFAULT_DISPLAY_NAME

    # Marks this provider as collecting a passcode via the host's
    # /auth/password form rather than an external OAuth authorize URL. The
    # auth-route layer duck-types this attribute.
    password_login = True

    def __init__(
        self,
        *,
        passcode: str,
        display_name: str = _DEFAULT_DISPLAY_NAME,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        if not passcode:
            raise ValueError("passcode must be non-empty")
        # Instance-level override of the class attribute so /login shows the
        # operator's label.
        self.display_name = display_name or _DEFAULT_DISPLAY_NAME
        self._passcode = passcode
        self._ttl = max(_MIN_TTL_SECONDS, int(ttl_seconds))
        # Derive a signing key from the passcode. Deterministic (sessions
        # survive restart) yet passcode-bound (rotation invalidates sessions).
        self._key = hashlib.pbkdf2_hmac(
            "sha256", passcode.encode("utf-8"), _KEY_CONTEXT, 200_000
        )

    # ---- public API (DashboardAuthProvider) -------------------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        # No external IDP. We only need a CSRF ``state`` nonce stashed in the
        # pkce cookie; the host's /auth/password form echoes it back and the
        # route checks it before accepting the passcode. ``redirect_uri`` (the
        # /auth/callback URL) is unused for this flow.
        _ = redirect_uri
        state = _b64url_no_pad(secrets.token_bytes(32))
        return LoginStart(
            redirect_url=_PASSWORD_PAGE_PATH,
            cookie_payload={"hermes_session_pkce": f"state={state}"},
        )

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session:
        # ``state`` (CSRF) is verified by the auth-route layer before this
        # call; ``code_verifier`` / ``redirect_uri`` are OAuth-only and unused.
        _ = (state, code_verifier, redirect_uri)
        # Constant-time compare so a wrong passcode can't be timing-probed.
        if not hmac.compare_digest(code or "", self._passcode):
            raise InvalidCodeError("incorrect passcode")
        return self._mint_session()

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        exp = self._verify_token(access_token)
        if exp is None:
            # Not ours, tampered, or expired — return None so the middleware
            # falls through to the next provider / redirects to login.
            return None
        return self._session(access_token, exp)

    def refresh_session(self, *, refresh_token: str) -> Session:
        # This provider issues no refresh tokens; re-auth is a passcode entry.
        _ = refresh_token
        raise RefreshExpiredError(
            "local passcode provider issues no refresh tokens; "
            "re-enter the passcode via /login."
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        # Stateless tokens — nothing to revoke. (Rotate the passcode to
        # invalidate every live session at once.) Best-effort no-op.
        _ = refresh_token
        return None

    # ---- internals --------------------------------------------------------

    def _mint_session(self) -> Session:
        exp = int(time.time()) + self._ttl
        return self._session(self._mint_token(exp), exp)

    def _mint_token(self, exp: int) -> str:
        payload = str(exp).encode("ascii")
        sig = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64url_no_pad(payload)}.{_b64url_no_pad(sig)}"

    def _verify_token(self, token: str) -> Optional[int]:
        """Return the token's ``exp`` if valid+unexpired, else ``None``."""
        if not token or "." not in token:
            return None
        payload_b64, _, sig_b64 = token.partition(".")
        try:
            payload = _b64url_decode(payload_b64)
            sig = _b64url_decode(sig_b64)
        except Exception:
            return None
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            exp = int(payload.decode("ascii"))
        except ValueError:
            return None
        if exp <= int(time.time()):
            return None
        return exp

    def _session(self, access_token: str, exp: int) -> Session:
        return Session(
            user_id=_LOCAL_USER_ID,
            email="",
            display_name=self.display_name,
            org_id="",
            provider=self.name,
            expires_at=exp,
            access_token=access_token,
            refresh_token="",
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def _load_config_local_auth_section() -> dict:
    """Return the ``dashboard.local_auth`` block from config.yaml, or ``{}``.

    Robust to load_config() raising and to the keys being absent / non-dict,
    mirroring the nous provider's defensive config access.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        logger.debug(
            "dashboard-auth-local: load_config() raised %s; "
            "falling back to env-only configuration",
            exc,
        )
        return {}
    section = cfg_get(cfg, "dashboard", "local_auth", default=None)
    return section if isinstance(section, dict) else {}


def _resolve_passcode(section: dict) -> str:
    env = os.environ.get("HERMES_DASHBOARD_LOCAL_PASSCODE", "").strip()
    if env:
        return env
    return str(section.get("passcode", "")).strip()


def _resolve_display_name(section: dict) -> str:
    env = os.environ.get("HERMES_DASHBOARD_LOCAL_DISPLAY_NAME", "").strip()
    if env:
        return env
    return str(section.get("display_name", "")).strip() or _DEFAULT_DISPLAY_NAME


def _resolve_ttl_seconds(section: dict) -> int:
    raw = os.environ.get("HERMES_DASHBOARD_LOCAL_TTL_SECONDS", "").strip()
    if not raw:
        raw = str(section.get("ttl_seconds", "")).strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        return max(_MIN_TTL_SECONDS, int(raw))
    except ValueError:
        logger.warning(
            "dashboard-auth-local: invalid ttl_seconds %r; using default %d",
            raw, _DEFAULT_TTL_SECONDS,
        )
        return _DEFAULT_TTL_SECONDS


def register(ctx) -> None:
    """Plugin entry — called by the plugin loader at startup.

    Registers the provider only when a passcode is configured (via
    ``HERMES_DASHBOARD_LOCAL_PASSCODE`` env var or
    ``dashboard.local_auth.passcode`` in config.yaml). Otherwise writes a
    specific reason to ``LAST_SKIP_REASON`` and no-ops, so loopback /
    ``--insecure`` operators are unaffected.
    """
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    section = _load_config_local_auth_section()
    passcode = _resolve_passcode(section)

    if not passcode:
        LAST_SKIP_REASON = (
            "HERMES_DASHBOARD_LOCAL_PASSCODE is not set (and "
            "dashboard.local_auth.passcode in config.yaml is empty). Set a "
            "shared passcode via either surface to enable the local-passcode "
            "auth gate, or pass --insecure to skip authentication entirely "
            "(not recommended on untrusted networks)."
        )
        logger.debug("dashboard-auth-local: %s", LAST_SKIP_REASON)
        return

    try:
        provider = LocalPasswordDashboardAuthProvider(
            passcode=passcode,
            display_name=_resolve_display_name(section),
            ttl_seconds=_resolve_ttl_seconds(section),
        )
    except ValueError as exc:
        LAST_SKIP_REASON = (
            f"LocalPasswordDashboardAuthProvider construction failed: {exc}"
        )
        logger.warning("dashboard-auth-local: %s", LAST_SKIP_REASON)
        return

    ctx.register_dashboard_auth_provider(provider)
    logger.info(
        "dashboard-auth-local: registered provider (display_name=%r, ttl=%ds)",
        provider.display_name, provider._ttl,
    )
