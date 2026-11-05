"""The hook-authoring fixture, with a project directory in which the test plants an unreadable document."""
from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("_hook_edit_base", Path(__file__).with_name("hook_edit_runtime.py"))
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
common = base.common
seed_valid = common.seed

def seed(app):
    # The test writes the unreadable document itself, once the runtime is up and has read its hooks.
    seed_valid(app)
    app.hook_config.path("project").parent.mkdir()


common.seed = seed
if __name__ == "__main__":
    common.history.main()
