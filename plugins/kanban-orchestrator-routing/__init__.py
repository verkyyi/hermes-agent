"""kanban-orchestrator-routing — front-desk routing invariant as a plugin hook.

Wires one behaviour via a single ``pre_tool_call`` hook:

* A ``kanban_create`` issued from the **DEFAULT front-desk profile** must be
  assigned to ``orchestrator``. The front desk routes *everything* through the
  orchestrator; it may not hand work straight to a ``worker-*`` lane.

This replaces a fork-local **inline edit** in
``tools/kanban_tools.py::_handle_create`` (docs/LOCAL_PATCHES.md #6). Moving it
to a plugin keeps the invariant out of a hot upstream file that conflicts on
every merge — the only remaining core touch is the (upstream-provided)
``pre_tool_call`` dispatch in ``agent/tool_executor.py``.

Scope (unchanged from the inline version):

* Fires only for the default front-desk profile: ``HERMES_PROFILE`` unset or
  ``"default"``, AND not dispatcher-spawned (``HERMES_KANBAN_TASK`` unset).
* Exempt — the orchestrator profile in BOTH modes (dispatcher-spawned, and its
  interactive routing surface ``HERMES_PROFILE=orchestrator``), plus every
  worker. Fanning out to worker lanes is their job.
* Reject-only: returns a ``block`` directive so the model re-issues with
  ``assignee='orchestrator'``. The executor wraps the message as
  ``{"error": <message>}`` — the same shape the old inline ``tool_error`` produced.

Absent / blank assignee is intentionally NOT handled here: the tool handler
still owns the "assignee is required" error, and masking it with the routing
message would be a worse error. We only enforce *which* assignee, never *whether*.

OPT-IN: standalone plugins load only when listed under ``plugins.enabled`` in
``~/.hermes/config.yaml``. Because this carries a safety invariant, enabling it
is the deployment's responsibility — see the README. Set
``KANBAN_ORCHESTRATOR_ROUTING_DISABLE=1`` to keep the plugin installed but inert.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

# Reject message — byte-for-byte the text the old inline guard passed to
# ``tool_error`` in ``_handle_create``.
_BLOCK_MESSAGE = (
    "front-desk tasks must be assigned to 'orchestrator' (it routes "
    "to the right worker). Re-create with assignee='orchestrator'."
)


def _disabled() -> bool:
    return os.environ.get("KANBAN_ORCHESTRATOR_ROUTING_DISABLE", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_frontdesk() -> bool:
    """True only for the DEFAULT front-desk profile (not dispatcher-spawned).

    Reads the same process-local env vars the inline guard read, so behaviour is
    identical regardless of which layer enforces it.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False  # dispatcher-spawned — orchestrator/worker, exempt
    profile = os.environ.get("HERMES_PROFILE", "").strip().lower()
    return profile in ("", "default")


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Block a front-desk ``kanban_create`` that targets a non-orchestrator lane."""
    if tool_name != "kanban_create" or _disabled():
        return None
    if not isinstance(args, dict):
        return None
    assignee = args.get("assignee")
    # Absent/blank assignee → let the tool handler raise "assignee is required".
    if not assignee or not str(assignee).strip():
        return None
    if _is_frontdesk() and str(assignee).strip().lower() != "orchestrator":
        return {"action": "block", "message": _BLOCK_MESSAGE}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
