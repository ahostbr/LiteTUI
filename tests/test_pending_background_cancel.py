"""Stop must cover the interval before a background shell publishes its handle."""
import asyncio
import json
import os
import sys
import threading

import pytest

from litetui import paths, tasks, tool_policy, ttyguard
from litetui.app import LiteTUI
from litetui.plugins import core_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_spawn", "before_attach"])
async def test_stop_pending_shell_prevents_side_effect_and_persists_killed(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")
    monkeypatch.setattr(LiteTUI, "connect", lambda self: None)
    entered, release = threading.Event(), threading.Event()
    children, kills = [], []
    sentinel = tmp_path / "must-not-run.txt"
    real_exe, real_popen, real_kill = core_tools.powershell_exe, ttyguard.popen, ttyguard.kill_tree

    def delayed_exe():
        entered.set()
        assert release.wait(10)
        return real_exe()

    def spawn(*args, **kwargs):
        if phase == "before_attach":
            args = ([sys.executable, "-c", f"import time; from pathlib import Path; time.sleep(15); Path({str(sentinel)!r}).write_text('ran')"],)
            kwargs["shell"] = False
        proc = real_popen(*args, **kwargs)
        children.append(proc)
        if phase == "before_attach":
            entered.set()
            assert release.wait(10)
        return proc

    def kill(*args, **kwargs):
        kills.append(args[0])
        return real_kill(*args, **kwargs)

    monkeypatch.setattr(ttyguard, "popen", spawn)
    monkeypatch.setattr(ttyguard, "kill_tree", kill)
    app = LiteTUI()
    if phase == "before_spawn":
        monkeypatch.setattr(core_tools, "powershell_exe", delayed_exe)
    app._active_tool_profile = tool_policy.AUTONOMOUS
    app._deliver_inbox = lambda message: None
    async with app.run_test(size=(100, 35)) as pilot:
        _, ok = await app._execute_tool("powershell", {
            "command": f"Set-Content -LiteralPath '{sentinel}' -Value ran", "background": True,
        })
        assert ok
        task = next(iter(app.bg_tasks.values()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert task.state == tasks.RUNNING and task.proc is None
            assert app._kill_background(task.id) is None
            assert task.state == tasks.KILLED
            rows = json.loads((tmp_path / tasks.STORE).read_text(encoding="utf-8"))
            assert next(row for row in rows if row["id"] == task.id)["state"] == tasks.KILLED
        finally:
            release.set()
            for _ in range(100):
                if task.ended is not None:
                    break
                await pilot.pause(.05)
            for proc in children:
                if proc.poll() is None:
                    real_kill(proc.pid, proc)
        assert task.ended is not None and task.state == tasks.KILLED
        assert not sentinel.exists()
        assert len(children) == (0 if phase == "before_spawn" else 1)
        assert kills == ([] if phase == "before_spawn" else [children[0].pid])
        assert all(proc.poll() is not None for proc in children)


def test_pending_stop_cannot_claim_foreign_task():
    task = tasks.new_task("powershell", {"command": "fixture"}, "convo")
    task.owner_pid = os.getpid() + 100000
    app = type("Host", (), {"bg_tasks": {task.id: task}, "_system": lambda *args: None})()
    assert LiteTUI._kill_background(app, task.id) is not None
    assert task.state == tasks.RUNNING


@pytest.mark.parametrize("tool", ["subagent", "trusted_provider"])
def test_pending_non_shell_stop_cannot_claim_uncancellable_work(tool):
    task = tasks.new_task(tool, {"command": "fixture"}, "convo")
    assert tasks.request_kill(task) == (False, None)
    assert task.state == tasks.RUNNING
