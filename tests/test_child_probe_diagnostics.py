import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/codex_child_lifecycle_probe.py"
    spec = importlib.util.spec_from_file_location("child_probe", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_gate_diagnostics_preserve_one_spawn_bound_and_never_record_payloads(tmp_path):
    probe = module()
    marker, diagnostics = tmp_path / "used", tmp_path / "diagnostics.jsonl"
    script = tmp_path / "gate.py"
    script.write_text(probe.gate_program(marker, diagnostics), encoding="utf-8")
    outcomes = []
    for name in ("spawn_agent", "spawn_agent", "tool_search", "wait", "PRIVATE_NAME"):
        result = subprocess.run([sys.executable, str(script)], input=json.dumps({
            "tool_name": name, "tool_input": {"prompt": "PRIVATE_PROMPT", "path": "PRIVATE_PATH"}}),
            text=True, capture_output=True, timeout=5, check=True)
        document = json.loads(result.stdout)
        outcomes.append("allow" if document == {} else document["hookSpecificOutput"]["permissionDecision"])
        assert "PRIVATE" not in result.stdout
    assert outcomes == ["allow", "deny", "deny", "allow", "deny"]
    raw = diagnostics.read_text(encoding="utf-8")
    assert "PRIVATE" not in raw and "tool_input" not in raw
    assert [json.loads(line) for line in raw.splitlines()] == [
        {"category": "spawn", "allowed": True},
        {"category": "spawn", "allowed": False},
        {"category": "search", "allowed": False},
        {"category": "wait", "allowed": True},
        {"category": "other", "allowed": False},
    ]


def test_unknown_protocol_values_collapse_to_other():
    probe = module()
    assert probe.enum_value("running", {"running", "completed"}) == "running"
    assert probe.enum_value("PRIVATE_STATUS", {"running", "completed"}) == "other"
    assert probe.enum_value({"text": "PRIVATE"}, {"running"}) == "other"
