"""Disposable static schema and normal real-app publication. No handler runs."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

from litetui import tool_schemas
from litetui.plugins import plugin_reload_ui

root = Path(os.environ["LITETUI_DATA_ROOT"])
spec = importlib.util.spec_from_file_location(
    "_plugin_boundaries", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
)
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed = common.seed
original_serve = common.serve
original = None
selected = None


def seed(app):
    global original, selected
    original_seed(app)
    # The shared child intentionally activates only two builtins. Capture the same
    # startup baseline as the reload plugin before the fixture creates a changed schema.
    original = app.plugins
    plugin_reload_ui.capture_baseline(app)
    eligible = plugin_reload_ui._eligible(app)
    selected = next(
        entry
        for entry in app.plugins.tools
        if entry.name in eligible and not plugin_reload_ui._has_placeholder(entry.spec)
    )
    directory = root / "private-schemas"
    directory.mkdir()
    schema = copy.deepcopy(selected.spec)
    schema["function"]["description"] = (
        "A reviewed replacement\nCurrent static tool description."
    )
    (directory / (selected.name + ".json")).write_text(
        json.dumps(schema), encoding="utf-8"
    )
    tool_schemas._package_dir = lambda: directory
    tool_schemas._legacy_dir = lambda: root / "empty-legacy"
    (root / "plugin-seed.json").write_text(
        json.dumps(
            {
                "name": selected.name,
                "before": selected.spec["function"]["description"],
                "after": schema["function"]["description"],
            }
        ),
        encoding="utf-8",
    )


async def serve(app):
    previous = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = str(root)
    try:
        await original_serve(app)
    finally:
        if previous is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = previous
        entry = next(e for e in app.plugins.tools if e.name == selected.name)
        (root / "plugin-after.json").write_text(
            json.dumps(
                {
                    "generation_changed": app.plugins is not original,
                    "same_handler": entry.run is selected.run,
                    "same_gate": entry.gate is selected.gate,
                    "same_policy": entry.policy == selected.policy,
                    "description": entry.spec["function"]["description"],
                }
            ),
            encoding="utf-8",
        )


common.seed = seed
common.history.serve = serve
if __name__ == "__main__":
    common.history.main()
