import importlib
import json
import subprocess
import sys
from pathlib import Path


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
        decisions.append(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])
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
