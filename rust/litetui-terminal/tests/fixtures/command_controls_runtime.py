"""Add exact canonical registrations to existing controlled boundary fixtures."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from litetui.plugins import (
    PluginContext,
    convo,
    misc,
    model_switch,
    plugin_reload_ui,
    scheduler_plugin,
    settings_ui,
)

spec = importlib.util.spec_from_file_location("_command_controls_base", Path(os.environ["RUST_COMMAND_CONTROLS_BASE"]))
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
original_seed = base.common.seed


def seed(app):
    original_seed(app)
    for module in (convo, misc, model_switch, plugin_reload_ui, scheduler_plugin, settings_ui):
        manifest = module.PLUGIN
        manifest.register(PluginContext(app, app.plugins, manifest.id))
    assert all(token in app.plugins.commands for token in (
        "/compact", "/reconnect", "/load", "/unload", "/engine", "/hooks", "/reload-plugins", "/job", "/plan"))


base.common.seed = seed
if __name__ == "__main__":
    base.common.history.main()
