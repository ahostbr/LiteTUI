"""Controlled actual owned CLI evidence for reviewed Rename and New."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/session_mutation_runtime.py"
DEFAULT_TITLE = "  Renamed  雪\ncurrent\tconversation  "


def launch(tmp_path, *, case="rename-saved", title=DEFAULT_TITLE, action="rename", phase="fresh", resume=None):
    backend = "claude" if case.startswith("claude-") else "codex" if case.startswith("codex-") else "custom"
    model = "gpt-6.1-sol" if backend == "codex" else "switch-old"
    thinking = "high" if backend == "codex" else "default"
    root = tmp_path / "data"
    if phase == "fresh":
        root.mkdir()
        settings = {"backend": backend, "default_model": model, "backend_chosen": True,
                    "thinking_level": thinking, "user_name_asked": True, "tools_enabled": False,
                    "mcp_enabled": False, "skills_enabled": False, "footer_task_manager": False,
                    "tts_enabled": False, "sidecar_enabled": False, "tool_policy_profile": "strict",
                    "autocompact_enabled": False, "tool_auto_background_s": 0,
                    "custom_base_url": "http://127.0.0.1:45120/v1", "custom_context_length": 8192,
                    "max_tokens_chat": 101, "max_tokens_tools": 202}
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_SESSION_MUTATION_SOURCE=str(runtime_source),
                       RUST_SESSION_MUTATION_DRIVER="self", RUST_SESSION_MUTATION_CASE=case,
                       RUST_SESSION_MUTATION_TITLE=title, RUST_SESSION_MUTATION_ACTION=action,
                       RUST_SESSION_MUTATION_PHASE=phase, LITETUI_DATA_ROOT=str(root),
                       PYTHONPATH=str(ROOT / "tests"), PYTHONUTF8="1",
                       PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input", "--backend", backend,
            "--model", model, "--thinking-level", thinking, "--tool-profile", "strict",
            "--mcp-servers", "none",
            ]
    if backend == "custom":
        args.extend(["--base-url", "http://127.0.0.1:45120/v1", "--server-timeout", "5", "--server-mode", "connect"])
    if backend == "codex":
        args.extend(["--codex-engine", "native"])
    if resume:
        args.extend(["--convo", resume])
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=80, check=False)
    (tmp_path / (phase + "-events.jsonl")).write_text(result.stdout, encoding="utf-8")
    (tmp_path / (phase + "-stderr.txt")).write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / ("session-mutation-" + phase + "-report.json")).read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert report["denied"] == []
    return report


@pytest.mark.parametrize("case", ["rename-saved", "rename-pending"])
@pytest.mark.parametrize("title,expected", [
    (DEFAULT_TITLE, "Renamed 雪 current conversation"),
    ("x" * 60, "x" * 60),
    ("x" * 61, "x" * 60 + "…"),
    ("😀" * 61, "😀" * 60 + "…"),
])
def test_rename_uses_canonical_review_and_durable_title_without_changing_history(tmp_path, case, title, expected):
    report = launch(tmp_path, case=case, title=title)
    proof = report["proof"]
    assert proof["review"]["title"] == proof["meta"]["name"] == expected
    assert proof["after_apply"]["session_id"] == report["before"]["session_id"]
    assert proof["after_apply"]["history"] == report["before"]["history"]
    assert not proof["after_apply"]["pending"]
    assert len(report["requests"]) == 1
    assert report["requests"][0]["messages"][-1]["content"] == "Explicit prompt after verified session change"


@pytest.mark.parametrize("case", ["new-saved", "new-pending"])
def test_new_uses_reserved_identity_and_old_transcript_is_unchanged(tmp_path, case):
    report = launch(tmp_path, case=case, action="new")
    assert report["proof"]["after_apply"]["session_id"] == report["proof"]["review"]["target_session_id"]
    assert report["before"]["session_id"] != report["after"]["session_id"]
    assert report["before"]["agent_id"] == report["after"]["agent_id"]
    assert report["old_unchanged"]
    assert all(row["role"] == "system" for row in report["proof"]["history_after_apply"])
    assert len(report["requests"]) == 1
    assert not any(row.get("content") == "History belonging to the original conversation"
                   for row in report["requests"][0]["messages"])


@pytest.mark.parametrize("case", ["validate-only", "cancel-review", "invalid", "foreign"])
def test_pure_review_refusal_and_cancellation_never_change_owned_data(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["initial_disk"] == report["final_disk"]
    assert report["before"] == report["after"] and not report["requests"]


@pytest.mark.parametrize("case", ["stale", "write-before", "write-after", "replay"])
def test_stale_partial_persistence_and_consumed_reviews_do_not_replay(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["proof"]["result"]["status"] == (
        "refused" if case == "stale" else "renamed" if case == "replay" else "failed")
    assert len(report["requests"]) == (1 if case == "replay" else 0)


@pytest.mark.parametrize("action", ["rename", "new"])
def test_verified_session_mutation_survives_restart_without_replaying_input(tmp_path, action):
    first = launch(tmp_path, case=action + "-saved", action=action)
    second = launch(tmp_path, action=action, phase="resume", resume=first["after"]["session_id"])
    assert second["before"]["session_id"] == first["after"]["session_id"]
    assert second["before"]["agent_id"] == first["after"]["agent_id"]
    if action == "rename":
        assert second["proof"]["resumed_meta"]["name"] == first["proof"]["review"]["title"]
    assert len(second["requests"]) == 1


@pytest.mark.parametrize("case", ["hooks-success", "hooks-failed-leave", "hooks-failed-start", "hooks-approval"])
def test_new_uses_real_hook_authorization_runner_and_settlement(tmp_path, case):
    report = launch(tmp_path, case=case, action="new")
    assert report["proof"]["review"]["effects"]["lifecycle_hooks"]
    result = report["proof"]["result"]
    hook_events = [row["document"] for row in report["events"] if row["effect"] == "hook"]
    if case == "hooks-success":
        assert result["status"] == "created" and result["lifecycle"]["hooks_completed"]
        assert len(hook_events) == 2 and len(report["requests"]) == 1
    else:
        assert result["status"] == "failed" and result["close_required"]
        assert not report["requests"]
        assert len(hook_events) == (0 if case == "hooks-approval" else 1 if case == "hooks-failed-leave" else 2)
        assert any(not row["allowed"] for row in report["proof"]["hook_results"].values())


def test_new_task_store_keeps_old_rows_bound_and_writes_new_rows_only_to_new_session(tmp_path):
    report = launch(tmp_path, case="task-store", action="new")
    rows = report["proof"]["task_rows"]
    old = next(value for key, value in rows.items() if report["before"]["session_id"] in key)
    new = next(value for key, value in rows.items() if report["after"]["session_id"] in key)
    assert [row["id"] for row in old] == ["fixture-old-task"]
    assert [row["id"] for row in new] == ["fixture-new-task"]
    assert report["old_unchanged"]


@pytest.mark.parametrize("case,action", [("claude-new", "new"), ("claude-rename", "rename"),
                                       ("claude-cleanup-failed", "new")])
def test_native_provider_cleanup_and_next_explicit_query_use_correct_session(tmp_path, case, action):
    report = launch(tmp_path, case=case, action=action)
    assert len(report["pre_requests"]) == 1
    old = report["pre_requests"][0]
    if case == "claude-cleanup-failed":
        assert report["proof"]["result"]["status"] == "failed"
        assert not report["requests"]
        return
    assert len(report["requests"]) == 1
    new = report["requests"][0]
    assert new["resume"] is None
    if action == "new":
        assert new["provider_session"] != old["provider_session"]
        assert new["session_id"] != old["session_id"]
        records = report["events"]
        closed = next(i for i, row in enumerate(records)
                      if row["effect"] == "native-close-end" and row["provider_session"] == old["provider_session"])
        queried = next(i for i, row in enumerate(records)
                       if row["effect"] == "native-query" and row["provider_session"] == new["provider_session"])
        assert closed < queried and report["old_unchanged"]
    else:
        assert new["provider_session"] == old["provider_session"]
        assert new["session_id"] == old["session_id"]


@pytest.mark.parametrize("case", ["held", "active"])
@pytest.mark.parametrize("action", ["rename", "new"])
def test_active_work_and_held_input_refuse_session_changes_without_losing_queue(tmp_path, case, action):
    report = launch(tmp_path, case=case, action=action)
    assert not report["proof"]["snapshot"]["actions"][action]["eligible"]
    assert report["before"] == report["after"] and not report["requests"]
    assert report["initial_disk"] == report["final_disk"]
    assert report["after"]["queue_count"] == (1 if case == "held" else 0)


def test_hook_configuration_drift_during_leave_cannot_publish_new_session(tmp_path):
    report = launch(tmp_path, case="hooks-drift", action="new")
    assert report["proof"]["result"]["status"] == "failed"
    assert report["proof"]["result"]["close_required"]
    assert report["before"]["session_id"] == report["after"]["session_id"]
    assert not report["requests"] and report["old_unchanged"]


@pytest.mark.parametrize("case", ["quit-during-apply", "eof-during-apply", "kill-refused"])
def test_shutdown_joins_real_hook_runner_before_lease_release_and_requires_cleanup_proof(tmp_path, case):
    report = launch(tmp_path, case=case, action="new")
    assert not report["pending_after_teardown"] and not report["requests"]
    assert report["proof"]["result"]["status"] == "failed"
    if case == "kill-refused":
        assert report["shutdown"] is None
        assert report["store_owned_after_teardown"]
        assert report["hook_process_codes"] == [None]
        assert report["proof"]["shutdown_refused"]
        assert not any(row["effect"] == "lease-release" for row in report["events"])
    else:
        assert report["shutdown"]["worker_settled"] and report["shutdown"]["pending_at_close"]
        assert report["shutdown"]["native_cleanup_settled"]
        assert report["shutdown"]["effects_verified"] is False
        assert report["hook_process_codes"] == [-15]
        assert not report["store_owned_after_teardown"]
        assert any(row["effect"] == "lease-release" for row in report["events"])
        assert not any(row.get("hook_alive") or row.get("pending") for row in report["events"]
                       if row["effect"] in {"shutdown-ack", "lease-release"})


@pytest.mark.parametrize("case", ["claude-quit-during-apply", "claude-eof-during-apply"])
def test_shutdown_waits_for_native_cleanup_before_ack_and_ownership_release(tmp_path, case):
    report = launch(tmp_path, case=case, action="new")
    assert len(report["pre_requests"]) == 1 and not report["requests"]
    assert report["before"]["session_id"] == report["after"]["session_id"]
    assert report["old_unchanged"] and not report["pending_after_teardown"]
    assert report["shutdown"]["native_cleanup_settled"] and report["shutdown"]["pending_at_close"]
    assert not report["store_owned_after_teardown"]
    events = report["events"]
    end = next(i for i, row in enumerate(events) if row["effect"] == "native-close-end")
    released = next(i for i, row in enumerate(events) if row["effect"] == "lease-release")
    assert end < released
    if case == "claude-quit-during-apply":
        ack = next(i for i, row in enumerate(events) if row["effect"] == "shutdown-ack")
        assert end < ack < released


@pytest.mark.parametrize("action", ["rename", "new"])
def test_actual_codex_adapter_retains_transport_but_starts_fresh_thread_only_after_explicit_new_prompt(tmp_path, action):
    report = launch(tmp_path, case="codex-" + action, action=action)
    assert len(report["pre_requests"]) == len(report["requests"]) == 1
    old, new = report["pre_requests"][0], report["requests"][0]
    assert report["proof"]["retained_transport_thread"] == old["thread_id"]
    assert_captured_codex_origins(report, 2)
    methods = [row["method"] for row in report["codex_calls"]]
    assert "thread/resume" not in methods
    assert methods.count("turn/start") == 2
    if action == "new":
        assert methods.count("thread/start") == 2 and new["thread_id"] != old["thread_id"]
        assert report["old_unchanged"]
        assert new["input"] == [{"type": "text", "text": "Explicit prompt after verified session change",
                                 "text_elements": []}]
    else:
        assert methods.count("thread/start") == 1 and new["thread_id"] == old["thread_id"]



def assert_captured_codex_origins(report, count):
    fields = ("session_id", "operation_id", "hook_id", "source", "execution_id")
    starts = {row["operation_id"]: row for row in report["wire"] if row["event"]["type"] == "turn_start"}
    native = [row for row in report["wire"] if row["event"]["type"] == "native_turn_started"]
    assert len(native) == count
    for row in native:
        origin = {key: starts[row["operation_id"]][key] for key in fields}
        assert all(value is not None for value in origin.values())
        assert {key: row.get(key) for key in fields} == origin
    return native


def test_codex_canonical_completion_hook_correction_keeps_outer_origin_and_uses_new_provider_turn(tmp_path):
    report = launch(tmp_path, case="codex-correction", action="new")
    assert len(report["pre_requests"]) == 1 and len(report["requests"]) == 2
    native = assert_captured_codex_origins(report, 3)
    assert native[-1]["execution_id"] == native[-2]["execution_id"]
    assert native[-1]["event"]["threadId"] == native[-2]["event"]["threadId"]
    assert native[-1]["event"]["turnId"] != native[-2]["event"]["turnId"]
    assert report["requests"][0]["input"] == [{"type": "text", "text": "Explicit prompt after verified session change",
                                              "text_elements": []}]
    assert report["requests"][1]["input"] == [{"type": "text", "text": "[internal hook correction]\nsession-observer: Controlled corrective followup",
                                              "text_elements": []}]
    assert [row["method"] for row in report["codex_calls"]].count("thread/start") == 2
    assert len([row for row in report["events"] if row["effect"] == "hook"]) == 2
