"""Native hooks: real processes, fail-closed gates, and isolated configuration."""
import asyncio
import json
import sys

import pytest

from litetui.lifecycle_hooks import Hook, HookConfig, HookError, run_hook


def definition(id="check", **extra):
    return {"id": id, "events": ["tool_before"], "mode": "gate",
            "executable": sys.executable, "argv": [], **extra}


def write(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "hooks": entries}), encoding="utf-8")


def test_scope_disabled_override_and_order(tmp_path):
    config = HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    write(config.global_path, [definition("a"), definition("b")])
    write(config.project_path, [definition("a", enabled=False), definition("c")])
    assert [h.id for h in config.snapshot().matching("tool_before")] == ["b", "c"]


def test_concurrent_independent_edits_merge_and_same_entry_conflicts(tmp_path):
    config = HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    write(config.global_path, [definition("a"), definition("b")])
    base = config.read("global")
    config.save("global", [base[0], Hook.parse(definition("b", timeout=12))], base)
    config.save("global", [Hook.parse(definition("a", timeout=15)), base[1]], base)
    assert [h.timeout for h in config.read("global")] == [15, 12]
    with pytest.raises(HookError, match="conflict"):
        config.save("global", [Hook.parse(definition("a", timeout=20)), base[1]], base)


def test_invalid_config_blocks_instead_of_dropping_gates(tmp_path):
    config = HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    write(config.global_path, [definition(timeout=0)])
    assert config.snapshot().error
    broken = config.global_path.read_bytes()
    with pytest.raises(HookError):
        config.repair("global", json.dumps({"version": 1, "hooks": []}), b"other bytes")
    config.repair("global", json.dumps({"version": 1, "hooks": []}), broken)
    assert not config.snapshot().error


def test_explicit_isolation_and_literal_tool_patterns(tmp_path, monkeypatch):
    config = HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    write(config.global_path, [definition(tools=["read[1]", "mcp_*?"])])
    state = config.snapshot()
    assert state.matching("tool_before", tool="read[1]")
    assert not state.matching("tool_before", tool="read1")
    assert state.matching("tool_before", tool="mcp_read")
    assert not state.matching("tool_before", tool="MCP_read")
    monkeypatch.setenv("LITETUI_HOOKS", "off")
    assert config.snapshot().disabled and not config.snapshot().hooks


def test_simultaneous_writers_preserve_independent_rows(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    config = HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    write(config.global_path, [definition("a"), definition("b")])
    baseline = config.read("global")
    barrier = Barrier(2)
    def edit(index):
        from dataclasses import replace
        rows = list(baseline)
        rows[index] = replace(rows[index], timeout=20 + index)
        barrier.wait()
        config.save("global", rows, baseline)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(edit, [0, 1]))
    assert [h.timeout for h in config.read("global")] == [20, 21]


@pytest.mark.parametrize("extra", [{"events": ["app_start"]}, {"timeout": 301},
                                   {"argv": "shell string"}, {"enabled": "false"}])
def test_invalid_definition(extra):
    with pytest.raises(HookError):
        Hook.parse(definition(**extra))


@pytest.mark.asyncio
async def test_real_script_receives_json_and_denies(tmp_path):
    script = tmp_path / "check.py"
    script.write_text('import json,sys\ne=json.load(sys.stdin)\n'
                      'print(json.dumps({"decision":"deny", "reason":e["event"]}))')
    hook = Hook.parse(definition(argv=[str(script)]))
    result = await run_hook(hook, {"event": "tool_before"}, tmp_path)
    assert not result.allowed
    assert result.reason == "tool_before"
    assert result.exit_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ['print("garbage")', 'raise SystemExit(2)',
                                   'print("x"*100000)'])
async def test_bad_gate_output_fails_closed(tmp_path, code):
    hook = Hook.parse(definition(argv=["-c", code]))
    result = await run_hook(hook, {}, tmp_path)
    assert not result.allowed
    assert len(result.stdout.encode()) <= 65536


@pytest.mark.asyncio
async def test_timeout(tmp_path):
    hook = Hook.parse(definition(argv=["-c", "import time; time.sleep(30)"], timeout=1))
    result = await asyncio.wait_for(run_hook(hook, {}, tmp_path), 8)
    assert result.timed_out and not result.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_fixture_child_tree(tmp_path, cancel):
    from litetui import router_record, ttyguard

    child_pid = tmp_path / "child.pid"
    code = ('import subprocess,sys,time,pathlib\n'
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])\n'
            f'pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n'
            'time.sleep(30)')
    hook = Hook.parse(definition(argv=["-c", code], timeout=2 if not cancel else 30))
    task = asyncio.create_task(run_hook(hook, {}, tmp_path))
    for _ in range(60):
        if child_pid.exists():
            break
        await asyncio.sleep(.025)
    assert child_pid.exists()
    pid = int(child_pid.read_text())
    assert router_record.pid_is_live(pid)
    try:
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)
        else:
            assert (await asyncio.wait_for(task, 8)).timed_out
        assert not router_record.pid_is_live(pid)
    finally:
        if router_record.pid_is_live(pid):
            ttyguard.kill_tree(pid)
