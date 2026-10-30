"""Owned CLI command and skill evidence; external inference/hardware is controlled."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/command_skill_runtime.py"


def launch(tmp_path, *, case="success", tools=False):
    root = tmp_path / "data"
    root.mkdir()
    settings = {"backend": "custom", "default_model": "switch-old", "backend_chosen": True,
                "thinking_level": "default", "user_name_asked": True,
                "tools_enabled": tools, "mcp_enabled": False, "skills_enabled": True, "skill_roots": [],
                "footer_task_manager": False, "tts_enabled": False, "sidecar_enabled": False,
                "tool_policy_profile": "strict", "autocompact_enabled": False,
                "tool_auto_background_s": 0, "custom_base_url": "http://127.0.0.1:45120/v1",
                "custom_context_length": 8192, "max_tokens_chat": 101, "max_tokens_tools": 202}
    (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_COMMAND_SKILL_SOURCE=str(runtime_source),
                       LITETUI_DATA_ROOT=str(root), RUST_COMMAND_SKILL_CASE=case,
                       RUST_COMMAND_SKILL_DRIVER="self", PYTHONPATH=str(ROOT / "tests"),
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input", "--backend", "custom",
            "--model", "switch-old", "--thinking-level", "default", "--tool-profile", "strict",
            "--mcp-servers", "none", "--server-timeout", "5", "--server-mode", "connect",
            "--base-url", "http://127.0.0.1:45120/v1"]
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=60, check=False)
    (tmp_path / "events.jsonl").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / "command-skill-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert report["denied"] == []
    assert report["callbacks"] == ([] if case != "padded-slash" else ["status_command"])
    return report


def exact_turn(report):
    prepared = report["prepared"]
    assert report["catalog_unchanged"] and report["prepare_unchanged"]
    assert prepared["prompt"] == report["expected_prompt"]
    assert prepared["prompt_sha256"] == hashlib.sha256(prepared["prompt"].encode("utf-8")).hexdigest()
    assert len(report["requests"]) == 1
    request = report["requests"][0]
    users = [message["content"] for message in request["messages"] if message.get("role") == "user"]
    assert users[-1] == prepared["prompt"]
    assert users[0] == "Preserved command history"
    assert request["profile"] == report["after"]["profile"] == report["before"]["profile"] == "strict"
    assert request["tools_enabled"] == report["before"]["tools_enabled"]
    assert "\r\n" in users[-1] and "\t" in users[-1] and users[-1].endswith("spaces  ")
    assert "${CLAUDE_SKILL_DIR}" not in users[-1]
    assert str(Path(prepared["skill"]["path"]).parent) in users[-1]
    envelopes = [event for event in report["events"] if event["type"] == "gui.event"
                 and event.get("event", {}).get("type") in {"turn_start", "turn_end"}]
    assert len(envelopes) == 2
    assert all(event["operation_id"] == "command-explicit-0" and event["source"] == "rpc"
               and event["session_id"] == report["before"]["session_id"] for event in envelopes)
    assert len({event["execution_id"] for event in envelopes}) == 1 and envelopes[0]["execution_id"]
    assert all(event["hook_id"] for event in envelopes)
    assert len([command for command in report["command_log"] if command["type"] == "prompt"]) == 1


@pytest.mark.parametrize("tools", [False, True])
def test_exact_skill_body_arguments_and_attended_authority_reach_one_explicit_turn(tmp_path, tools):
    exact_turn(launch(tmp_path, tools=tools))


@pytest.mark.parametrize("case,expected,wrong", [
    ("case-ambiguity", "EXACT CASE BODY", "WRONG CASE BODY"),
    ("folder-ambiguity", "EXACT FOLDER BODY", "WRONG FOLDER BODY"),
])
def test_selected_row_loads_exact_file_without_legacy_case_or_folder_rematch(tmp_path, case, expected, wrong):
    report = launch(tmp_path, case=case)
    exact_turn(report)
    assert expected in report["prepared"]["body"] and wrong not in report["prepared"]["body"]


@pytest.mark.parametrize("case", ["prepare-only", "cancel", "encoded-budget"])
def test_preparation_and_cancellation_do_not_append_or_send(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["prepare_unchanged"] and report["catalog_unchanged"]
    assert report["before"] == report["after"] and report["initial_disk"] == report["final_disk"]
    assert not report["requests"] and report["prepare_attempts"] == 1
    assert not any(command["type"] == "prompt" for command in report["command_log"])
    if case == "encoded-budget":
        encoded = json.dumps(report["prepared"], ensure_ascii=True).encode("utf-8")
        assert 512 * 1024 < len(encoded) < 1024 * 1024


def test_actual_alias_catalog_routes_without_registered_callback_execution(tmp_path):
    report = launch(tmp_path, case="routes")
    assert [route["native_action"] for route in report["routes"]] == [
        "settings", "settings", "settings", "quit", "thinking", "conversations", "skills"]
    assert len({route["row_id"] for route in report["routes"][:3]}) == 1
    assert report["before"] == report["after"] and report["initial_disk"] == report["final_disk"]
    assert not report["requests"] and report["prepare_attempts"] == 0


@pytest.mark.parametrize("case", ["unknown", "unsupported", "control-args", "mixed", "raw-ambiguous",
                                  "stale-generation", "stale-policy", "foreign-agent", "foreign-session"])
def test_unknown_unsupported_argument_and_stale_routes_never_dispatch(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["error"] and not report["routes"] and not report["requests"]
    assert report["prepare_attempts"] == 0
    assert report["before"]["history"] == report["after"]["history"]
    assert report["initial_disk"] == report["final_disk"]
    assert len([command for command in report["command_log"] if command["type"] == "gui.commands.resolve"]) == 1


@pytest.mark.parametrize("case", ["invalid-utf8", "oversized", "binding-expansion", "file-drift", "args-oversized"])
def test_unstable_invalid_and_oversized_skill_inputs_refuse_without_prompt_side_effects(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["error"] and "prepared" not in report and not report["requests"]
    assert report["prepare_attempts"] == 1
    assert report["before"] == report["after"] and report["initial_disk"] == report["final_disk"]


def test_preparation_reads_current_file_version_without_claiming_catalog_bound_file_bytes(tmp_path):
    report = launch(tmp_path, case="file-updated")
    exact_turn(report)
    assert "Fresh file revision" in report["prepared"]["body"]
    raw = Path(report["prepared"]["skill"]["path"]).read_bytes()
    assert report["prepared"]["file"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["prepared"]["file"]["bytes"] == len(raw)


@pytest.mark.parametrize("case", ["whitespace-only", "padded-slash"])
def test_exact_rpc_preservation_retains_empty_and_command_detection(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert not report["requests"] and report["before"]["history"] == report["after"]["history"]
    if case == "whitespace-only":
        assert any(event["type"] == "submit_refused" and event["reason"] == "empty" for event in report["events"])


@pytest.mark.parametrize("case", ["raw-skill", "shadowed-skill"])
def test_raw_skill_routes_and_shadowed_command_name_load_exact_selected_file(tmp_path, case):
    report = launch(tmp_path, case=case)
    exact_turn(report)
    assert report["route"]["native_action"] == "skill_prepare"
    assert report["route"]["arguments"] == report["prepared"]["arguments"]
    if case == "shadowed-skill":
        row = next(row for row in report["catalog"]["entries"] if row["kind"] == "skill" and row["title"] == "settings")
        assert row["completion_order"] is None
        assert "EXACT SHADOWED BODY" in report["prepared"]["body"]
