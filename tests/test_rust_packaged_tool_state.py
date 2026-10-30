"""Packaged helper state stays outside code; browser/desktop/network/process are inert."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import litetui

ROOT = Path(litetui.__file__).resolve().parents[2]


def load(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def external_tripwires(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unexpected browser, network or process action")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    return forbidden


@pytest.fixture
def helpers(tmp_path, monkeypatch, external_tripwires):
    root = tmp_path / "retained state"
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(root))
    bridge = load("bridge", ROOT / "tools/chrome-bridge/bridge.py", monkeypatch)
    relay = load("packaged_relay", ROOT / "tools/chrome-bridge/relayctl.py", monkeypatch)
    chrome = load("packaged_chrome", ROOT / "src/litetui/chrome_tool.py", monkeypatch)
    assert not root.exists(), "Reading code must not initialize helper state"
    return root, bridge, relay, chrome


@pytest.mark.parametrize("override", [None, ""])
def test_unset_data_root_keeps_standalone_paths_without_writes(monkeypatch, external_tripwires, override):
    if override is None:
        monkeypatch.delenv("LITETUI_DATA_ROOT", raising=False)
    else:
        monkeypatch.setenv("LITETUI_DATA_ROOT", override)
    bridge = load("bridge", ROOT / "tools/chrome-bridge/bridge.py", monkeypatch)
    relay = load("packaged_relay", ROOT / "tools/chrome-bridge/relayctl.py", monkeypatch)
    chrome = load("packaged_chrome", ROOT / "src/litetui/chrome_tool.py", monkeypatch)
    assert bridge.STATE_DIR == bridge.HERE
    assert Path(bridge.SHOT_DIR) == Path(bridge.HERE) / "shots"
    assert Path(bridge.PID_FILE) == Path(bridge.HERE) / "relay.pid"
    assert Path(relay.OUT_LOG) == Path(bridge.HERE) / "relay.out.log"
    assert Path(relay.ERR_LOG) == Path(bridge.HERE) / "relay.err.log"
    assert chrome.SHOT_DIR == chrome.RELAY_DIR


def test_browser_screenshots_use_retained_state_and_keep_code_unchanged(helpers, monkeypatch):
    root, bridge, _relay, chrome = helpers
    assert chrome.SHOT_DIR == root / "chrome-bridge"
    image = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 2, 3)
    client = bridge.Chrome.__new__(bridge.Chrome)
    client._call = lambda *_args, **_kwargs: {"data_url": "data:image/png;base64," + base64.b64encode(image).decode()}
    automatic = client.screenshot()
    assert Path(automatic["path"]).is_relative_to(root / "chrome-bridge/shots")
    assert automatic["width"] == 2 and automatic["height"] == 3
    assert Path(automatic["path"]).read_bytes() == image
    def capture(argv, **_kwargs):
        assert argv[0] == "shot"
        return json.dumps(client.screenshot(path=argv[1]))
    monkeypatch.setattr(chrome, "_run", capture)
    result = chrome.run({"action": "shot"})
    assert "Saved" in result and (root / "chrome-bridge/chrome-shot.png").read_bytes() == image
    assert "NOT changed" in chrome.run({"action": "shot"})


def test_relay_log_lifecycle_uses_state_and_code_directory_only_for_execution(helpers, monkeypatch):
    root, bridge, relay, _chrome = helpers
    answers = iter([("down", ""), ("up", "")])
    monkeypatch.setattr(relay, "probe", lambda *_args: next(answers))
    monkeypatch.setattr(relay.time, "sleep", lambda *_args: None)
    calls = []
    def start(argv, **kwargs):
        assert argv == [sys.executable, str(ROOT / "tools/chrome-bridge/bridge.py"), "serve"]
        assert kwargs["cwd"] == bridge.HERE
        assert Path(kwargs["stdout"].name) == root / "chrome-bridge/relay.out.log"
        assert Path(kwargs["stderr"].name) == root / "chrome-bridge/relay.err.log"
        calls.append((argv, kwargs))
        return SimpleNamespace()
    monkeypatch.setattr(relay.subprocess, "Popen", start)
    try:
        assert relay.start() == "started"
        assert len(calls) == 1
        calls[0][1]["stdout"].write("inert diagnostic")
        calls[0][1]["stdout"].flush()
    finally:
        for _, kwargs in calls:
            kwargs["stdout"].close()
            kwargs["stderr"].close()
    assert relay.clear_logs() == "logs cleared"
    assert Path(relay.OUT_LOG).read_text() == Path(relay.ERR_LOG).read_text() == ""


@pytest.mark.asyncio
async def test_relay_pid_is_written_only_after_both_bindings_and_cleanup_keeps_foreign_pid(helpers, monkeypatch):
    root, bridge, _relay, _chrome = helpers
    entries = []
    @asynccontextmanager
    async def bound(_handler, _host, port, **_kwargs):
        entries.append(port)
        assert not Path(bridge.PID_FILE).exists()
        yield None
    async def idle():
        return
    monkeypatch.setattr(bridge, "serve", bound)
    relay = bridge.Relay(quiet=True)
    monkeypatch.setattr(relay, "_keepalive", idle)
    task = asyncio.create_task(relay._main())
    try:
        for _ in range(100):
            if Path(bridge.PID_FILE).exists():
                break
            await asyncio.sleep(0)
        assert len(entries) == 2
        assert Path(bridge.PID_FILE) == root / "chrome-bridge/relay.pid"
        assert Path(bridge.PID_FILE).read_text() == str(os.getpid())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    # run() owns the cleanup rule, independent of an actual socket/event loop.
    def stop(coro):
        coro.close()
    with monkeypatch.context() as patch:
        patch.setattr(bridge.asyncio, "run", stop)
        relay.run()
        assert not Path(bridge.PID_FILE).exists()
        Path(bridge.PID_FILE).write_text("foreign")
        relay.run()
        assert Path(bridge.PID_FILE).read_text() == "foreign"


def test_relay_module_launch_handles_relocated_paths_with_apostrophes(helpers, monkeypatch, tmp_path):
    _root, _bridge, _relay, chrome = helpers
    monkeypatch.setattr(chrome, "RELAY_DIR", tmp_path / "Ryan's packaged tools")
    seen = []
    def capture(argv, **_kwargs):
        code = argv[2]
        compile(code, "<relocated relay launch>", "exec")
        import ast
        tree = ast.parse(code)
        value = tree.body[1].value.args[1]
        assert ast.literal_eval(value) == str(chrome.RELAY_DIR)
        seen.append(code)
        return SimpleNamespace(returncode=0, stdout="inert status", stderr="")
    monkeypatch.setattr(chrome.ttyguard, "run", capture)
    assert chrome._relay("status()") == "inert status" and len(seen) == 1


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_desktop_helper_default_and_explicit_output_without_capturing_desktop(tmp_path, monkeypatch, external_tripwires, override, explicit):
    root = tmp_path / "retained state"
    if override:
        monkeypatch.setenv("LITETUI_DATA_ROOT", str(root))
    else:
        monkeypatch.delenv("LITETUI_DATA_ROOT", raising=False)
    module = load("packaged_screenshot", ROOT / "tools/pccontrol/screenshot.py", monkeypatch)
    # Preserve legacy layout in a disposable script directory.
    code = tmp_path / "legacy helper"
    code.mkdir()
    monkeypatch.setattr(module, "__file__", str(code / "screenshot.py"))
    target = tmp_path / "explicit" / "selected.png"
    argv = ["screenshot.py"] + (["--out", str(target)] if explicit else [])
    monkeypatch.setattr(sys, "argv", argv)
    fake = SimpleNamespace(width=2, height=3, save=lambda path: path.write_bytes(b"inert image"))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(screenshot=lambda **_kw: fake))
    module.main()
    expected = target if explicit else (root / "pccontrol" if override else code) / "last_shot.png"
    assert expected.read_bytes() == b"inert image"
    if override:
        assert list(code.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell helper")
@pytest.mark.parametrize("override", [None, "", "retained"])
@pytest.mark.parametrize("monitor", [-1, 2])
@pytest.mark.parametrize("explicit", [False, True])
def test_powershell_screenshot_paths_without_loading_desktop_capture(tmp_path, override, monitor, explicit):
    """Evaluate only the two path functions extracted from the shipped script AST."""
    script_dir = tmp_path / "legacy helper"
    data_root = tmp_path / "retained state"
    selected = tmp_path / "explicit output" / "selected.jpg"
    options = {
        "Monitor": monitor,
        "Output": str(selected) if explicit else "",
        "ScriptDirectory": str(script_dir),
        "DataRoot": str(data_root) if override == "retained" else override,
    }
    values = tmp_path / "path-options.json"
    values.write_text(json.dumps(options), encoding="utf-8")
    wrapper = tmp_path / "paths-only.ps1"
    wrapper.write_text(
        "param([string]$Script,[string]$Options)\n"
        "$ErrorActionPreference='Stop'\n"
        "function Add-Type { throw 'Desktop assembly loading is forbidden' }\n"
        "function New-Object { throw 'Desktop object construction is forbidden' }\n"
        "$tokens=$null; $errors=$null\n"
        "$tree=[System.Management.Automation.Language.Parser]::ParseFile($Script,[ref]$tokens,[ref]$errors)\n"
        "if ($errors.Count) { throw ($errors | Out-String) }\n"
        "$definitions=@($tree.FindAll({ param($node)\n"
        " $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and\n"
        " $node.Name -in @('Resolve-ScreenshotOutputPath','Initialize-ScreenshotOutputDirectory')\n"
        "},$false))\n"
        "if ($definitions.Count -ne 2) { throw 'Missing bounded path functions' }\n"
        "foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }\n"
        "$settings=Get-Content -LiteralPath $Options -Raw | ConvertFrom-Json -AsHashtable\n"
        "$path=Resolve-ScreenshotOutputPath @settings\n"
        "$parent=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($path))\n"
        "$before=[IO.Directory]::Exists($parent)\n"
        "Initialize-ScreenshotOutputDirectory -Path $path\n"
        "@{path=$path; before=$before; after=[IO.Directory]::Exists($parent)} | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper),
         "-Script", str(ROOT / "tools/pccontrol/screenshot.ps1"), "-Options", str(values)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    expected_dir = data_root / "pccontrol" if override == "retained" else script_dir
    expected = selected if explicit else expected_dir / ("all.jpg" if monitor < 0 else f"mon{monitor}.jpg")
    assert Path(report["path"]) == expected
    assert report["before"] is False and report["after"] is True
    assert expected.parent.is_dir() and not expected.exists()
