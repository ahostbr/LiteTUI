"""Real owned engine management; synthetic external OS/provider boundaries only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/engine_management_runtime.py"


def launch(tmp_path, *, backend="strata", case="success", resume=None):
    root = tmp_path / "data"
    root.mkdir(exist_ok=resume is not None)
    settings = {"backend": backend, "default_model": "engine-model", "backend_chosen": True,
                "thinking_level": "default", "user_name_asked": True,
                "tools_enabled": False, "mcp_enabled": False, "skills_enabled": False,
                "footer_task_manager": False, "tts_enabled": False, "sidecar_enabled": False,
                "tool_policy_profile": "strict", "autocompact_enabled": False,
                "tool_auto_background_s": 0, "max_tokens_chat": 101, "max_tokens_tools": 202}
    if resume is None:
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_ENGINE_MANAGEMENT_SOURCE=str(runtime_source),
                       RUST_ENGINE_MANAGEMENT_DRIVER="self", RUST_ENGINE_MANAGEMENT_CASE=case,
                       LITETUI_DATA_ROOT=str(root), PYTHONPATH=str(ROOT / "tests"),
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input", "--backend", backend,
            "--model", "engine-model", "--thinking-level", "default", "--tool-profile", "strict",
            "--mcp-servers", "none", "--server-timeout", "5", "--server-mode", "connect"]
    if resume is not None:
        args.extend(["--convo", resume])
    if case != "blocked":
        args.append("--engine-management")
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=80, check=False)
    (tmp_path / "events.jsonl").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / "engine-management-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert report["denied"] == []
    assert all(proc["returncode"] is not None for proc in report["processes"])
    return report


@pytest.mark.parametrize("backend", ["ninfer", "strata"])
def test_explicit_start_readback_prompt_stop_returns_to_management(tmp_path, backend):
    report = launch(tmp_path, backend=backend)
    assert report["ready"]["launch_status"] == "management"
    assert len(report["requests"]) == 1
    request = report["requests"][0]
    assert request["backend"] == backend and request["model"] == "engine-model"
    assert request["profile"] == "strict" and request["tools_enabled"] is False
    assert request["messages"][-1]["content"] == "Explicit prompt after engine start"
    assert len([call for call in report["calls"] if call["effect"] == "spawn"]) == 1
    assert report["after"]["session_id"] == report["before"]["session_id"]


@pytest.mark.parametrize("backend", ["ninfer", "strata"])
@pytest.mark.parametrize("case", ["blocked", "barrier", "cancel-review", "stale", "plan-drift", "attached", "registered-foreign"])
def test_unadmitted_or_unowned_actions_never_spawn_or_infer(tmp_path, backend, case):
    report = launch(tmp_path, backend=backend, case=case)
    assert not report["requests"] and not report["processes"]
    assert report["before"]["history"] == report["after"]["history"]
    assert report["before"]["pending"] == report["after"]["pending"]
    if case == "blocked":
        assert report["ready"]["launch_status"] == "blocked"


@pytest.mark.parametrize("backend", ["ninfer", "strata"])
@pytest.mark.parametrize("case", ["resource-memory", "resource-sibling", "one-shot-stale"])
def test_canonical_resource_refusal_never_waits_for_hidden_approval_or_starts(tmp_path, backend, case):
    report = launch(tmp_path, backend=backend, case=case)
    assert not report["requests"] and not report["processes"]
    receipts = [row["result"] for row in report["results"] if row.get("result", {}).get("kind") == "engine_action"]
    assert len(receipts) == 1 and receipts[0]["status"] == "refused"
    if case == "resource-sibling":
        assert receipts[0]["error_code"] == "resource_confirmation_required"
    assert not any(event.get("type") == "model_load_requested" for event in report["events"])


@pytest.mark.parametrize("backend", ["ninfer", "strata"])
@pytest.mark.parametrize("case", ["retained-start", "retained-stop", "cancel-start", "model-drift", "replay", "legacy-escape", "source-health-drift", "source-model-drift"])
def test_retained_handles_cancellation_and_consumed_reviews_keep_source_authority(tmp_path, backend, case):
    report = launch(tmp_path, backend=backend, case=case)
    assert len(report["processes"]) == 1
    assert len(report["requests"]) == (1 if case in {"retained-stop", "replay", "legacy-escape"} else 0)
    assert report["after"]["session_id"] == report["before"]["session_id"]


@pytest.mark.parametrize("backend", ["ninfer", "strata"])
def test_management_restart_restores_exact_owned_history_before_explicit_start(tmp_path, backend):
    first = launch(tmp_path, backend=backend)
    session = first["after"]["session_id"]
    second = launch(tmp_path, backend=backend, resume=session)
    assert second["before"]["session_id"] == second["after"]["session_id"] == session
    assert second["before"]["agent_id"] == first["after"]["agent_id"]
    assert second["ready"]["management_instance"] != first["ready"]["management_instance"]
    assert second["ready"]["engine_generation"] == 0
    prior = [row for row in first["after"]["history"] if row["role"] != "system"]
    restored = [row for row in second["before"]["history"] if row["role"] != "system"]
    assert restored == prior and restored
    assert len(second["requests"]) == 1
    assert len(second["after"]["history"]) == len(second["before"]["history"]) + 2


@pytest.mark.parametrize("backend", ["ninfer", "strata"])
def test_stopped_resume_retains_held_input_until_explicit_cancel_then_start(tmp_path, backend):
    first = launch(tmp_path, backend=backend, case="seed-held")
    assert len(first["after"]["pending"]) == 1
    second = launch(tmp_path, backend=backend, case="held-input", resume=first["after"]["session_id"])
    assert second["before"]["pending"] == first["after"]["pending"]
    assert not second["after"]["pending"]
    assert len(second["requests"]) == 1
    assert second["requests"][0]["messages"][-1]["content"] == "Explicit prompt after engine start"
    assert not any(row.get("content") == "NEVER AUTOMATICALLY REPLAY THIS INPUT"
                   for row in second["after"]["history"])
