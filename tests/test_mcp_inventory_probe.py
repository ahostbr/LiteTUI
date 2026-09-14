import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def probe_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("codex_mcp_turn_probe")


def test_mcp_gate_allows_only_synthetic_tools_and_discovery(tmp_path, monkeypatch):
    module = probe_module(monkeypatch)
    log, script = tmp_path / "log", tmp_path / "gate.py"
    script.write_text(module.gate_program(log), encoding="utf-8")
    names = ["mcp__catalog_probe__alpha", "mcp__catalog_probe__beta", "tool_search",
             "spawn_agent", "exec_command", "PRIVATE_TOOL"]
    decisions = []
    for name in names:
        result = subprocess.run([sys.executable, str(script)], input=json.dumps({
            "tool_name": name, "tool_input": {"value": "PRIVATE_ARGUMENT"}}),
            text=True, capture_output=True, check=True, timeout=5)
        document = json.loads(result.stdout)
        decisions.append("allow" if document == {} else document["hookSpecificOutput"]["permissionDecision"])
        assert "PRIVATE" not in result.stdout
    assert decisions == ["allow"] * 3 + ["deny"] * 3
    assert "PRIVATE" not in log.read_text()


def test_fixture_enforces_changed_schema_and_only_records_enumerated_metadata(tmp_path, monkeypatch):
    module = probe_module(monkeypatch)
    state, counts, fixture = tmp_path / "state", tmp_path / "counts", tmp_path / "fixture.py"
    fixture.write_text(module.FIXTURE, encoding="utf-8")
    results = []
    for version, name, value in [(1, "alpha", "PRIVATE"), (2, "alpha", "PRIVATE"),
                                  (2, "alpha", 7), (2, "beta", None)]:
        state.write_text(str(version))
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": {"value": value}}}
        result = subprocess.run([sys.executable, str(fixture), str(state), str(counts)],
                                input=json.dumps(request) + "\n", text=True,
                                capture_output=True, check=True, timeout=5)
        results.append(json.loads(result.stdout)["result"]["isError"])
    assert results == [False, True, False, False]
    assert "PRIVATE" not in counts.with_suffix(".calls").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_probe_requires_verified_trust_before_model_work(tmp_path, monkeypatch, enabled):
    module = probe_module(monkeypatch)

    class Server:
        config_overrides = ()

        def __init__(self):
            self.calls = []

        async def request(self, method, params):
            self.calls.append(method)
            return [{"command": "synthetic", "key": "key", "currentHash": "hash",
                     "trustStatus": "trusted" if self.config_overrides else "untrusted",
                     "enabled": enabled}]

        async def close(self):
            self.calls.append("close")

        async def start(self):
            self.calls.append("start")

    server = Server()
    if enabled:
        assert await module.trust_gate(server, tmp_path, "synthetic") == {
            "trusted_before": False, "trusted_after": True, "enabled": True,
        }
    else:
        with pytest.raises(AssertionError, match="not trusted and enabled"):
            await module.trust_gate(server, tmp_path, "synthetic")
    assert server.calls == ["hooks/list", "close", "start", "hooks/list"]
    assert server.config_overrides == ('hooks.state={"key"={trusted_hash="hash"}}',)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell invocation contract")
def test_gate_command_executes_in_powershell_with_spaces(tmp_path, monkeypatch):
    module = probe_module(monkeypatch)
    script, log = tmp_path / "gate with spaces.py", tmp_path / "log"
    script.write_text(module.gate_program(log), encoding="utf-8")
    shell = shutil.which("powershell.exe")
    assert shell
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", module.gate_command(script)],
        input=json.dumps({"tool_name": "mcp__catalog_probe__alpha"}), text=True,
        capture_output=True, check=True, timeout=10,
        env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]})
    assert json.loads(result.stdout) == {}
    assert json.loads(log.read_text()) == {"category": "alpha", "allowed": True}
