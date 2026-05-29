"""HermesBench dashboard plugin — backend API.

Mounted at /api/plugins/hermesbench/ by the dashboard plugin system. Read-only
window onto the consolidated-benchmark trend store
(``$HERMES_HOME/hermesbench.db``); never runs a benchmark or mutates state.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/trend")
async def trend(limit: int = 30):
    """Most-recent benchmark runs (newest first), each with per-suite results."""
    try:
        from evals.hermesbench import store

        runs = store.recent_runs(limit=max(1, min(limit, 365)))
    except Exception as exc:  # store missing / unreadable — empty, not 500
        return {"runs": [], "error": str(exc)}
    return {"runs": runs}
