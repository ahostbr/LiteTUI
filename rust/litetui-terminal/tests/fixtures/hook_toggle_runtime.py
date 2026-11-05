"""Seed only private global/project hook files and prohibit executing any configured hook."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from litetui import lifecycle_hooks as hooks

root = Path(os.environ["LITETUI_DATA_ROOT"])
spec = importlib.util.spec_from_file_location("_hook_fixture", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"]))
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
    paths[1].parent.mkdir()
    os.environ["LITETUI_HOOKS"] = "on"
    global_row = {"id": "audit-file", "events": ["tool_before"], "executable": "fixture-audit.exe",
                  "argv": ["PRIVATE_ARGUMENT"], "env": {"PRIVATE_KEY": "PRIVATE_VALUE"}}
    project = {**global_row, "enabled": False, "mode": "gate", "sources": ["rpc"]}
    for path, row in zip(paths, (global_row, project), strict=True):
        path.write_bytes(json.dumps({"version": 1, "hooks": [row]}, indent=2).encode() + bytes([13, 10]))
    (root / "hook-inputs.json").write_text(json.dumps([str(path) for path in paths]), encoding="utf-8")


async def reject_hook(*args, **kwargs):
    invocations.append("unexpected")
    raise AssertionError("No configured process may run in this fixture")


async def serve(app):
    original_run = hooks.run_hook
    hooks.run_hook = reject_hook
    try:
        await original_serve(app)
    finally:
        hooks.run_hook = original_run
        names = ("global", "project")
        (root / "hook-after.json").write_text(json.dumps({
            "scopes": {scope: [{ "id": h.id, "enabled": h.enabled} for h in app.hook_config.read(scope)] for scope in names},
            "effective": [{"id": h.id, "scope": h.scope, "enabled": h.enabled} for h in app.hook_config.snapshot().hooks],
            "executed": invocations,
        }), encoding="utf-8")


common.seed = seed
common.history.serve = serve
if __name__ == "__main__":
    common.history.main()
