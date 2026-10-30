"""Owned CLI settings-editor evidence with only provider/hardware seams controlled."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/settings_edit_runtime.py"


def launch(tmp_path, *, case="success", tools=False, backend="custom", phase="fresh", resume=None):
    root = tmp_path / "data"
    if phase == "fresh":
        root.mkdir()
        settings = {"backend": backend, "default_model": "switch-old", "backend_chosen": True,
                    "thinking_level": "default", "user_name_asked": True,
                    "tools_enabled": tools, "mcp_enabled": False, "skills_enabled": False,
                    "footer_task_manager": False, "tts_enabled": False, "sidecar_enabled": False,
                    "tool_policy_profile": "strict", "autocompact_enabled": False,
                    "tool_auto_background_s": 0, "custom_base_url": "http://127.0.0.1:45120/v1",
                    "custom_context_length": 8192, "codex_native_engine": backend == "codex",
                    "max_tokens_chat": 101, "max_tokens_tools": 202}
        if case == "model-override":
            settings["model_infer_overrides"] = {"switch-old": {"max_tokens": 313}}
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_SETTINGS_EDIT_SOURCE=str(runtime_source),
                       LITETUI_DATA_ROOT=str(root), RUST_SETTINGS_EDIT_CASE=case,
                       RUST_SETTINGS_EDIT_PHASE=phase,
                       RUST_SETTINGS_EDIT_FIELD="max_tokens_tools" if tools else "max_tokens_chat",
                       RUST_SETTINGS_EDIT_VALUE="257", PYTHONPATH=str(ROOT / "tests"),
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    if case == "env-override":
        environment["LITETUI_MAX_TOKENS"] = "337"
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input",
            "--backend", backend, "--model", "switch-old", "--thinking-level", "default",
            "--tool-profile", "strict", "--mcp-servers", "none", "--server-timeout", "5"]
    if backend == "custom":
        args.extend(["--server-mode", "connect", "--base-url", "http://127.0.0.1:45120/v1"])
    if backend == "codex":
        args.extend(["--codex-engine", "native"])
    if case == "cli-override":
        args.extend(["--max-tokens", "307"])
    if resume:
        args.extend(["--convo", resume])
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=55, check=False)
    (tmp_path / (phase + "-events.jsonl")).write_text(result.stdout, encoding="utf-8")
    (tmp_path / (phase + "-stderr.txt")).write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / ("settings-edit-" + phase + "-report.json")).read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert report["denied"] == []
    return report


@pytest.mark.parametrize("tools", [False, True])
def test_saved_conversation_budget_controls_next_explicit_request_and_survives_restart(tmp_path, tools):
    report = launch(tmp_path, tools=tools)
    field = "max_tokens_tools" if tools else "max_tokens_chat"
    other = "max_tokens_chat" if tools else "max_tokens_tools"
    assert report["snapshot_unchanged"] and report["validate_unchanged"]
    assert report["result"]["status"] == "saved" and not report["result"]["close_required"]
    assert report["result"]["mutated"]
    assert report["after_save"][field] == 257
    assert report["after_save"][other] == report["before"][other]
    assert report["saved_document"]["execution"][field] == 257
    assert len(report["requests"]) == 1 and report["requests"][0]["max_tokens"] == 257
    assert any(message.get("content") == "Preserved settings history" for message in report["requests"][0]["messages"])
    assert report["readback"]["fields"][field]["saved"] == report["readback"]["fields"][field]["effective"] == 257
    resumed = launch(tmp_path, tools=tools, phase="resume", resume=report["after"]["session_id"])
    assert resumed["before"]["session_id"] == report["after"]["session_id"]
    assert resumed["before"]["agent_id"] == report["after"]["agent_id"]
    assert resumed["before"][field] == 257
    assert len(resumed["requests"]) == 1 and resumed["requests"][0]["max_tokens"] == 257


@pytest.mark.parametrize("case", ["validate-only", "invalid"])
def test_validation_is_pure_and_never_dispatches(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["snapshot_unchanged"] and report["validate_unchanged"]
    assert report["initial_disk"] == report["final_disk"]
    assert report["before"] == report["after"] and not report["requests"]
    assert ("validation_error" in report) == (case == "invalid")


@pytest.mark.parametrize("case,tools", [("cli-override", False), ("cli-override", True),
                                       ("model-override", False), ("env-override", True)])
def test_real_request_precedence_disables_misleading_edits(tmp_path, case, tools):
    report = launch(tmp_path, case=case, tools=tools)
    field = "max_tokens_tools" if tools else "max_tokens_chat"
    assert report["snapshot"]["fields"][field]["editable"] is False
    assert report["snapshot"]["fields"][field]["reason"]
    assert report["validation_error"] and report["validate_unchanged"]
    assert report["initial_disk"] == report["final_disk"] and not report["requests"]


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_native_provider_managed_output_budgets_are_not_editable(tmp_path, backend):
    report = launch(tmp_path, backend=backend)
    assert all(not row["editable"] and row["reason"] for row in report["snapshot"]["fields"].values())
    assert report["validation_error"] and report["validate_unchanged"]
    assert report["initial_disk"] == report["final_disk"] and not report["requests"]


def test_stale_revision_is_refused_without_applying_or_dispatching(tmp_path):
    report = launch(tmp_path, case="stale")
    assert report["result"]["status"] == "refused"
    assert not report["result"]["mutated"] and not report["result"]["close_required"]
    assert report["after"] == report["before"] and not report["requests"]
    assert report["saved_document"]["execution"]["max_tokens_chat"] == 118


@pytest.mark.parametrize("case", ["persistence-refused", "write-then-error", "runtime-failed", "runtime-raised"])
def test_persistence_and_runtime_faults_never_claim_effective_success(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["result"]["status"] != "saved"
    assert report["after_save"]["max_tokens_chat"] == report["before"]["max_tokens_chat"]
    if case in {"runtime-failed", "persistence-refused"}:
        assert len(report["requests"]) == 1
        assert report["requests"][0]["max_tokens"] == report["before"]["max_tokens_chat"]
    else:
        assert not report["requests"]
    if case == "runtime-failed":
        assert report["result"]["status"] == "partial"
        assert report["saved_document"]["execution"]["max_tokens_chat"] == 257
    if case in {"write-then-error", "runtime-raised"}:
        assert report["result"]["close_required"] and report["result"]["mutated"]


def test_fresh_pending_conversation_can_save_budget_before_first_prompt(tmp_path):
    report = launch(tmp_path, case="empty")
    assert report["snapshot_unchanged"] and report["validate_unchanged"]
    assert report["before"]["history_count"] == report["after_save"]["history_count"]
    assert report["before"]["visible_history_count"] == report["after_save"]["visible_history_count"] == 0
    assert report["snapshot"]["revisions"]["conversation"] == "absent"
    assert report["result"]["status"] == "saved"
    assert report["saved_document"]["execution"]["max_tokens_chat"] == 257
    assert len(report["requests"]) == 1 and report["requests"][0]["max_tokens"] == 257
