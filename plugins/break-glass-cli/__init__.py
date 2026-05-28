"""break-glass-cli — register the `hermes break-glass` subcommand as a plugin.

Replaces a fork-local inline edit in ``hermes_cli/main.py`` (which imported
``break_glass.build_parser`` and called it against the top-level subparsers)
with a ``register_cli_command`` registration. The command implementation stays
in ``hermes_cli/break_glass.py``; this plugin only wires it into the CLI.

See docs/LOCAL_PATCHES.md #14.

OPT-IN / robustness note: standalone plugins load only when listed in
``plugins.enabled``, and registration runs inside ``discover_plugins()`` whose
failures the CLI swallows. break-glass is an emergency self-repair tool, so the
deploy config should keep ``break-glass-cli`` enabled — otherwise the subcommand
silently disappears exactly when the runtime is degraded. This tradeoff was
accepted deliberately to take the wiring out of the hot ``main.py`` file.
"""

from __future__ import annotations

from hermes_cli.break_glass import (
    BREAK_GLASS_DESCRIPTION,
    BREAK_GLASS_HELP,
    configure_parser,
)


def register(ctx) -> None:
    # configure_parser already calls parser.set_defaults(func=cmd_break_glass),
    # so no handler_fn is needed here.
    ctx.register_cli_command(
        name="break-glass",
        help=BREAK_GLASS_HELP,
        description=BREAK_GLASS_DESCRIPTION,
        setup_fn=configure_parser,
    )
