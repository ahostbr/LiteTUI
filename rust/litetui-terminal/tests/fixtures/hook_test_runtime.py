"Run only an owned disposable Python fixture through the real hook authorization and process runner."
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from litetui import lifecycle_hooks as hooks
from litetui import tool_policy, ttyguard

root = Path(os.environ["LITETUI_DATA_ROOT"])
spec = importlib.util.spec_from_file_location("_hook_test_fixture", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"]))
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed = common.seed
# Avoid the venv launcher spawning a second interpreter for this disposable fixture.
interpreter = sys._base_executable
children = []


def seed(app):
    original_seed(app)
    workspace = root / "test-workspace"
    workspace.mkdir()
    script = workspace / "sample.py"
    script.write_text(
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "event = json.load(sys.stdin)\n"
        "assert os.environ['SYNTHETIC_KEY'] == 'SYNTHETIC_SECRET'\n"
        "with Path('executed.jsonl').open('a', encoding='utf8') as f: f.write(json.dumps(event)+'\\n')\n"
        "if sys.argv[1] == 'slow': time.sleep(4)\n"
        "sys.stdout.write('SYNTHETIC_SECRET' * 7000)\n",
        encoding="utf-8")
    app._hook_workspace = workspace
    app.hook_config = hooks.HookConfig(*hooks.config_paths(workspace))
    cfg = app.hook_config
    assert all(cfg.path(name).is_relative_to(root) for name in ("global", "project"))
    cfg.project_path.parent.mkdir()
    os.environ["LITETUI_HOOKS"] = "on"
    rows = []
    rules = []
    for name, arg in [("authorized", "fast"), ("requires-confirm", "fast"), ("timeout", "slow"), ("quit", "slow")]:
        row = {"id": name, "enabled": False, "executable": interpreter,
               "events": ["tool_before", "prompt_before"], "argv": [str(script), arg],
               "timeout": 1 if name == "timeout" else 8, "env": {"SYNTHETIC_KEY": "SYNTHETIC_SECRET"}}
        rows.append(row)
        hook = hooks.Hook.parse(row, "project")
        if name != "requires-confirm":
            action = hook.approval_name(workspace)
            args = {"command": json.dumps([hook.executable, *hook.argv]), "cwd": str(workspace), "env": dict(hook.env)}
            decision = tool_policy.evaluate("strict", tool_policy.SHELL_POLICY, args, workspace, tool_name=action)
            assert decision.action == tool_policy.CONFIRM
            rules.append(tool_policy.rule_key(action, decision.capabilities))
    app.settings.tool_always_allow = rules
    cfg.project_path.write_text(json.dumps({"version": 1, "hooks": rows}), encoding="utf-8")


original_serve = common.serve
source_popen = ttyguard.popen
system_popen = subprocess.Popen


def check_call(argv, kwargs):
    workspace = root / "test-workspace"
    assert isinstance(argv, list) and len(argv) == 3 and argv[0] == interpreter
    assert Path(argv[1]) == workspace / "sample.py" and argv[2] in {"slow", "fast"}
    assert Path(kwargs["cwd"]) == workspace and kwargs["env"]["SYNTHETIC_KEY"] == "SYNTHETIC_SECRET"
    assert not kwargs.get("shell", False)


async def serve(app):
    # The shared boundary denies every process. Permit exactly this disposable
    # argv/cwd through the real ttyguard path, including its Windows Job handle.
    from unittest.mock import patch

    def contained(argv, **kwargs):
        check_call(argv, kwargs)
        assert kwargs["kill_on_close"] is True
        child = source_popen(argv, **kwargs)
        children.append(child)
        return child

    def spawn(argv, **kwargs):
        check_call(argv, kwargs)
        return system_popen(argv, **kwargs)

    ordinary_run = ttyguard.run

    def private_fallback(cmd, **kwargs):
        if cmd[:2] != ["taskkill", "/PID"]:
            return ordinary_run(cmd, **kwargs)
        # An outer test Job can prevent nested Job assignment. This generated
        # script cannot spawn children. Only permit its original process handle
        # at the fallback, never killing by numeric PID or enumerating the host.
        proc = next(p for p in children if cmd == ["taskkill", "/PID", str(p.pid), "/T", "/F"])
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        return subprocess.CompletedProcess(cmd, 0)

    with (patch.object(ttyguard, "popen", contained), patch.object(subprocess, "Popen", spawn),
          patch.object(ttyguard, "run", private_fallback)):
        try:
            await original_serve(app)
        finally:
            # The test must finish with no live fixture even if the OS declines
            # the canonical tree-kill proof. The slow script itself ends in 4s.
            for child in children:
                child.wait(timeout=6)
            (root / "tested-exits.json").write_text(json.dumps([p.returncode for p in children]), encoding="utf8")


common.history.serve = serve
common.seed = seed
if __name__ == "__main__":
    common.history.main()
