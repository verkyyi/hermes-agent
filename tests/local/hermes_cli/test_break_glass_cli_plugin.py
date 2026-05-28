"""break-glass-cli plugin — the `hermes break-glass` subcommand is registered via
``register_cli_command`` instead of an inline edit in ``hermes_cli/main.py``
(docs/LOCAL_PATCHES.md #14).

Covers: the refactored ``configure_parser`` builds a working parser that
dispatches to ``cmd_break_glass``; the plugin's ``register`` wires the command
with the shared help/description; and the real plugin manager surfaces the
command when the plugin is enabled (opt-in gating).
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import yaml

from hermes_cli import break_glass as bg

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_INIT = _REPO_ROOT / "plugins" / "break-glass-cli" / "__init__.py"


def _load_plugin():
    """Import the hyphen-named bundled plugin module by file path."""
    spec = importlib.util.spec_from_file_location("break_glass_cli_plugin", _PLUGIN_INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- refactored parser ----------------------------------------------------


def test_configure_parser_dispatches_to_cmd_break_glass():
    """configure_parser populates an already-created parser and sets the handler."""
    root = argparse.ArgumentParser(prog="hermes")
    subs = root.add_subparsers(dest="command")
    bg.configure_parser(subs.add_parser("break-glass"))

    ns = root.parse_args(["break-glass", "smoke", "--worker", "--timeout", "30"])
    assert ns.func is bg.cmd_break_glass
    assert ns.break_glass_action == "smoke"
    assert ns.worker is True and ns.timeout == 30


def test_configure_parser_covers_all_actions():
    root = argparse.ArgumentParser(prog="hermes")
    subs = root.add_subparsers(dest="command")
    bg.configure_parser(subs.add_parser("break-glass"))
    for action in ("diagnose", "test", "verify", "repair"):
        ns = root.parse_args(["break-glass", action, "--json"])
        assert ns.break_glass_action == action and ns.json is True


def test_build_parser_backcompat_shim_still_works():
    """The retained build_parser() shim creates + configures the subparser."""
    root = argparse.ArgumentParser(prog="hermes")
    subs = root.add_subparsers(dest="command")
    bg.build_parser(subs)
    ns = root.parse_args(["break-glass", "diagnose", "--json"])
    assert ns.func is bg.cmd_break_glass and ns.break_glass_action == "diagnose"


# --- plugin registration --------------------------------------------------


def test_register_wires_command_with_shared_text():
    captured = {}

    class _Ctx:
        def register_cli_command(self, **kwargs):
            captured.update(kwargs)

    _load_plugin().register(_Ctx())
    assert captured["name"] == "break-glass"
    assert captured["setup_fn"] is bg.configure_parser
    assert captured["help"] == bg.BREAK_GLASS_HELP
    assert captured["description"] == bg.BREAK_GLASS_DESCRIPTION


# --- real plugin-manager wiring + opt-in gating ---------------------------


def _fresh_manager(monkeypatch, tmp_path, enabled):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": enabled}}), encoding="utf-8"
    )
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    plugins_mod.discover_plugins()
    return plugins_mod


def test_command_registered_when_enabled(monkeypatch, tmp_path):
    plugins_mod = _fresh_manager(monkeypatch, tmp_path, ["break-glass-cli"])
    cmds = plugins_mod.get_plugin_manager()._cli_commands
    assert "break-glass" in cmds
    assert cmds["break-glass"]["setup_fn"] is bg.configure_parser


def test_command_absent_when_not_enabled(monkeypatch, tmp_path):
    """Opt-in gating: disabled → the subcommand is not registered. This is the
    robustness tradeoff of moving an emergency tool behind plugins.enabled."""
    plugins_mod = _fresh_manager(monkeypatch, tmp_path, [])
    assert "break-glass" not in plugins_mod.get_plugin_manager()._cli_commands
