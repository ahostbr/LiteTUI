"""Seed private global/project hook scopes, forbid executing any hook, and record what authoring left behind."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from litetui import lifecycle_hooks as hooks

root = Path(os.environ["LITETUI_DATA_ROOT"])
spec = importlib.util.spec_from_file_location("_hook_edit_fixture", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"]))
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed = common.seed
original_serve = common.serve
invocations = []


def seed(app):
    original_seed(app)
    workspace = root / "hook-workspace"
    workspace.mkdir()
    app._hook_workspace = workspace
    app.hook_config = hooks.HookConfig(*hooks.config_paths(workspace))
    paths = [app.hook_config.path(scope) for scope in ("global", "project")]
    assert all(path.is_relative_to(root) for path in paths)
    os.environ["LITETUI_HOOKS"] = "on"
    # Only the global scope has a file. The project directory does not exist yet: creating the first
    # project hook must create both the directory and the document.
    private = {"id": "audit-file", "events": ["tool_before"], "executable": "fixture-audit.exe",
               "argv": ["PUBLIC_ARGUMENT"], "env": {"PRIVATE_KEY": "PRIVATE_VALUE"}, "cwd": "PRIVATE_DIRECTORY"}
    paths[0].write_bytes(json.dumps({"version": 1, "hooks": [private]}, indent=2).encode() + bytes([13, 10]))
    (root / "hook-inputs.json").write_text(json.dumps([str(path) for path in paths]), encoding="utf-8")


async def reject_hook(*args, **kwargs):
    invocations.append("unexpected")
    raise AssertionError("Authoring must never run a configured hook")


async def serve(app):
    original_run = hooks.run_hook
    hooks.run_hook = reject_hook
    try:
        await original_serve(app)
    finally:
        hooks.run_hook = original_run
        names = ("global", "project")
        (root / "hook-edit-after.json").write_text(json.dumps({
            "scopes": {scope: [h.document() for h in app.hook_config.read(scope)] for scope in names},
            "effective": [{"id": h.id, "scope": h.scope, "enabled": h.enabled} for h in app.hook_config.snapshot().hooks],
            "executed": invocations,
        }), encoding="utf-8")


common.seed = seed
common.history.serve = serve
if __name__ == "__main__":
    common.history.main()
