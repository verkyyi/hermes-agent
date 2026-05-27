"""Shared fixtures for the local-patch test tree.

``tests/local/`` mirrors the upstream test directory layout but holds only
Hermes-local tests, kept out of the official test files so upstream merges
don't conflict on test additions.  Because these tests live in a different
directory than their upstream counterparts, the directory-scoped
``conftest.py`` fixtures/setup from ``tests/gateway/``, ``tests/hermes_cli/``
and ``tests/run_agent/`` are NOT inherited here — only the project-root
``tests/conftest.py`` is.  This module re-exposes the pieces the extracted
tests rely on.
"""

from __future__ import annotations

import pytest

# Gateway local tests import ``gateway.run`` / ``gateway.config`` which pull in
# ``gateway.platforms.telegram`` (and discord).  The upstream gateway conftest
# installs comprehensive sys.modules mocks at collection time; replicate that
# here so the local gateway tests pass in isolation too (importing the module
# runs its top-level ``_ensure_*_mock()`` calls as a side effect).
from tests.gateway.conftest import _ensure_telegram_mock, _ensure_discord_mock

_ensure_telegram_mock()
_ensure_discord_mock()

# Re-export the hermes_cli dispatcher fixture so local kanban tests that
# request ``all_assignees_spawnable`` resolve it here.
from tests.hermes_cli.conftest import all_assignees_spawnable  # noqa: F401


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch):
    """Mirror tests/run_agent/conftest.py: short-circuit retry backoff.

    Autouse across the whole local tree; harmless for tests that don't
    import run_agent (the patch target simply isn't present).
    """
    try:
        import run_agent
    except ImportError:
        return
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
