"""Backend-switch subprocess evidence; actual source ownership with synthetic externals."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/backend_switch_runtime.py"


def launch(tmp_path, *, case="success", initial="custom", target="lmstudio", thinking="default"):
    root = tmp_path / "data"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({
        "backend": initial, "default_model": "switch-old", "backend_chosen": True,
        "thinking_level": thinking, "user_name_asked": True, "tools_enabled": False,
        "mcp_enabled": False, "skills_enabled": False, "footer_task_manager": False,
        "tts_enabled": False, "sidecar_enabled": False, "tool_policy_profile": "strict",
        "autocompact_enabled": False, "tool_auto_background_s": 0,
        "custom_base_url": "http://127.0.0.1:45120/v1", "custom_context_length": 8192,
        "lm_host": "http://127.0.0.1:45121", "llama_host": "http://127.0.0.1:45122",
        "llama_attach_hosts": ["http://127.0.0.1:45129"],
    }), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_BACKEND_SWITCH_SOURCE=str(runtime_source), LITETUI_DATA_ROOT=str(root),
                       RUST_BACKEND_SWITCH_CASE=case, RUST_BACKEND_SWITCH_TARGET=target,
                       PYTHONPATH=str(ROOT / "tests"), PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       PYTHONDONTWRITEBYTECODE="1")
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input",
            "--backend", initial, "--model", "switch-old", "--thinking-level", thinking,
            "--tool-profile", "strict", "--mcp-servers", "none", "--server-timeout", "5"]
    if initial == "custom":
        args.extend(["--server-mode", "connect", "--base-url", "http://127.0.0.1:45120/v1"])
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=50, check=False)
    (tmp_path / "switch-events.jsonl").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "switch-stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / "switch-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert not report["denied"]
    return report, root


@pytest.mark.parametrize("initial,target", [("custom", "lmstudio"), ("custom", "llamacpp"), ("claude", "lmstudio")])
def test_actual_switch_preserves_owned_history_and_routes_only_next_explicit_prompt(tmp_path, initial, target):
    report, _root = launch(tmp_path, initial=initial, target=target)
    result, before, after = report["result"], report["before"], report["after"]
    # /backend switches the backend only; /model picks the model (Ryan 2026-11-12).
    assert result["status"] == "switched" and result["mutated"] and not result["close_required"]
    assert result["model"] is None and result["connected"] is True and result["offer_model_picker"] is True
    assert before["session_id"] == after["session_id"] and before["agent_id"] == after["agent_id"]
    assert after["backend"] == after["runtime_backend"] == target
    assert after["model"] is None and after["runtime_model"] == ""
    picked = report["after_pick"]
    assert picked["backend"] == target and picked["model"] == picked["runtime_model"] == "switch-new"
    assert after["thinking"] == before["thinking"] == "default"
    assert after["cli_model"] is None and after["cli_backend"] is None
    assert report["old_client_closed"]
    settings = report["state"]["settings"]
    metadata = {row["name"]: row for row in settings["metadata"]}
    assert metadata.keys() == settings["requested"].keys() == settings["effective"].keys()
    assert all(type(row["secret"]) is bool for row in metadata.values())
    assert metadata["groq_api_key"]["secret"] is True
    assert metadata["custom_api_key_env"]["secret"] is False
    assert not any(name.startswith("ninfer_") for name in metadata)
    assert settings["requested"]["backend"] == settings["effective"]["backend"] == target
    assert len(report["requests"]) == 1
    contents = [message.get("content") for message in report["requests"][0]["messages"]]
    assert "History before backend switch" in contents and "Explicit next prompt" in contents
    saved = report["saved_agent"]
    assert saved["execution"]["backend"] == target and saved["execution"]["model"] is None
    assert report["requests"][0]["backend"] == target and report["requests"][0]["model"] == "switch-new"
    if initial == "claude":
        effects = [row["effect"] for row in report["calls"]]
        assert effects.count("native-close-start") == effects.count("native-close-end") == 1


@pytest.mark.parametrize("case", ["stale", "foreign", "held"])
def test_prepublication_refusal_preserves_identity_and_transcript(tmp_path, case):
    report, _root = launch(tmp_path, case=case)
    assert report["result"]["status"] == "refused"
    assert not report["result"]["mutated"] and not report["result"]["close_required"]
    assert report["before"] == report["after"]
    assert report["home_unchanged"] and report["transcript_unchanged"]
    assert report["requests"] == []
    if case == "held":
        assert report["held"][0]["content"] == "UNSENT HELD INPUT"


def test_postpublication_failure_requires_close_and_blocks_late_prompt(tmp_path):
    report, _root = launch(tmp_path, case="persistence-failed")
    assert report["result"]["status"] == "failed"
    assert report["result"]["mutated"] and report["result"]["close_required"]
    assert report["requests"] == [] and report["failure_latch"]
    assert all(message.get("content") != "Must not dispatch" for message in report["messages"])


@pytest.mark.parametrize("case,target", [("connection-failed", "lmstudio"), ("llama-dead", "llamacpp")])
def test_a_backend_that_does_not_answer_leaves_the_seat_on_it_disconnected(tmp_path, case, target):
    # App.apply_backend_change: the switch is saved and Python stays on the new backend,
    # disconnected, with no model chosen. Connect-only: nothing is spawned, started or loaded.
    report, _root = launch(tmp_path, case=case, target=target)
    result = report["result"]
    assert result["status"] == "switched" and result["mutated"] and not result["close_required"]
    assert result["connected"] is False and result["offer_model_picker"] is False
    assert report["after"]["backend"] == report["after"]["runtime_backend"] == target
    assert report["after"]["model"] is None and report["after"]["runtime_model"] == ""
    assert "model_pick" not in report and not report["failure_latch"]
    assert report["requests"] == []


def test_native_cleanup_failure_is_reported_and_the_switch_goes_on_as_in_python(tmp_path):
    # claude_backend.settle_close returns cleanup failures as text so every caller says what
    # happened and carries on; /backend (App.apply_backend_change) does not stop on them.
    report, _root = launch(tmp_path, case="cleanup-failed", initial="claude")
    assert report["result"]["status"] == "switched" and not report["result"]["close_required"]
    assert sum(row["effect"] == "native-close-end" for row in report["calls"]) == 1
    assert [row["backend"] for row in report["requests"]] == ["lmstudio"]


def test_switch_never_silently_changes_incompatible_thinking(tmp_path):
    report, _root = launch(tmp_path, case="thinking", thinking="high")
    # The level last used on lmstudio comes back with it, else the current one stays: "high".
    assert report["result"]["status"] == "switched" and not report["result"]["close_required"]
    assert report["before"]["thinking"] == report["after"]["thinking"] == "high"
    assert report["after_pick"]["model"] == "switch-new"
    assert report["requests"] == []


def test_missing_target_model_fails_closed_without_publishing_fallback(tmp_path):
    report, _root = launch(tmp_path, case="missing-model")
    assert report["result"]["status"] == "refused" and not report["result"]["close_required"]
    assert not report["result"]["mutated"] and report["home_unchanged"]
    assert report["before"] == report["after"] and report["requests"] == []
