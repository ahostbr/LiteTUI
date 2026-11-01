"""Actual owned saved-history pages, full context and negotiated startup contracts."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/history_runtime.py"


def launch(tmp_path, *, case="many", phase="fresh", resume=None, allow_failure=False):
    root = tmp_path / "data"
    native = case.startswith("codex-")
    lmstudio = case.startswith("lmstudio-")
    backend, model, thinking = ("codex", "gpt-6.1-sol", "high") if native else (
        "lmstudio" if lmstudio else "custom", "switch-old", "default")
    if phase == "fresh":
        root.mkdir()
        settings = {"backend": backend, "default_model": model, "backend_chosen": True,
                    "thinking_level": thinking, "user_name_asked": True, "codex_native_engine": native,
                    "tools_enabled": False, "mcp_enabled": False, "skills_enabled": False,
                    "footer_task_manager": False, "tts_enabled": False, "sidecar_enabled": False,
                    "tool_policy_profile": "strict", "autocompact_enabled": False,
                    "tool_auto_background_s": 0, "custom_base_url": "http://127.0.0.1:45120/v1",
                    "lm_host": "http://127.0.0.1:45120", "custom_context_length": 1000000, "max_tokens_chat": 101, "max_tokens_tools": 202,
                    "theme_name": "textual-dark", "custom_themes": {}}
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_HISTORY_SOURCE=str(runtime_source), RUST_HISTORY_DRIVER="self",
                       RUST_HISTORY_CASE=case, RUST_HISTORY_PHASE=phase, LITETUI_DATA_ROOT=str(root),
                       PYTHONPATH=str(ROOT / "tests"), PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input",
            "--history-projection-version", "1", "--backend", backend, "--model", model,
            "--tool-profile", "strict", "--mcp-servers", "none",
            "--server-mode", "connect", "--server-timeout", "5"]
    if not lmstudio:
        args.extend(["--thinking-level", thinking])
    args.extend(["--codex-engine", "native"] if native else [
        "--base-url", "http://127.0.0.1:45120" if lmstudio else "http://127.0.0.1:45120/v1"])
    if resume:
        args.extend(["--convo", resume])
    completed = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True, text=True,
                               encoding="utf-8", timeout=120, check=False)
    (tmp_path / (phase + "-events.jsonl")).write_text(completed.stdout, encoding="utf-8")
    (tmp_path / (phase + "-stderr.txt")).write_text(completed.stderr, encoding="utf-8")
    if not allow_failure:
        assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((root / ("history-" + phase + "-report.json")).read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert report["denied"] == []
    report["returncode"] = completed.returncode
    report["stderr"] = completed.stderr
    return report


def texts(report):
    return [block["text"] for block in report["walk"]["blocks"] if block["text"] is not None]


def assert_read_only_inspection(report, *, read_only=False):
    assert report["inspection_unchanged"]
    assert report["descriptor"]["projection_version"] == 1
    assert report["descriptor"]["read_only"] is read_only
    assert report["walk"]["maximum_wire_bytes"] <= 1024 * 1024
    assert all(observation["wire_bytes"] <= 1024 * 1024 for observation in report["observations"])
    for request in report["requests"]:
        assert request["complete_prepared_context"]
        assert request["context_sha256"] == request["prepared_sha256"]
        assert request["wire_row_hashes"] == request["expected_row_hashes"]
        assert request["wire_roles"] == request["saved_roles"] + ["user"]
    assert report["walk"]["pages"] >= 1
    assert len(report["walk"]["blocks"]) == report["descriptor"]["counts"]["blocks"]


def test_more_than_1000_messages_and_100_visible_blocks_survive_pages_and_restart(tmp_path):
    report = launch(tmp_path)
    assert_read_only_inspection(report)
    assert report["descriptor"]["counts"] == {"messages": 1203, "system": 1, "non_system": 1202, "blocks": 1202}
    assert report["walk"]["pages"] > 1
    assert texts(report)[0] == "Saved user 0: repeated prompt"
    assert texts(report)[-1] == "Saved answer 600"
    assert len(report["requests"]) == 1
    request = report["requests"][0]
    assert len(request["users"]) == 602
    assert request["users"][0] == "Saved user 0: repeated prompt"
    assert request["users"][-1] == "Explicit prompt after saved history inspection"
    assert request["backend"] == "custom" and request["model"] == "switch-old"
    assert request["profile"] == "strict" and not request["tools_enabled"]
    resumed = launch(tmp_path, phase="resume", resume=report["after"]["session_id"])
    assert_read_only_inspection(resumed)
    assert resumed["before"]["session_id"] == report["after"]["session_id"]
    assert resumed["before"]["agent_id"] == report["after"]["agent_id"]
    assert not resumed["startup_bubbles"], "Negotiated resume must not eagerly materialize saved Textual cards"
    transcript = "/conversations/" + report["after"]["session_id"] + "/convo.jsonl"
    resumed_reads = [row for row in resumed["startup_reads"] if row["path"].replace("\\", "/").endswith(transcript)]
    assert resumed_reads and all(row["max_bytes"] == 8 * 1024 * 1024 for row in resumed_reads)
    assert len(resumed["requests"]) == 1


@pytest.mark.parametrize("case,unit", [
    ("giant", "giant text 雪\r\n"), ("wire-heavy", "\x00\x1b\u202e\r\n"), ("astral", "🛰️雪e\u0301\n"),
])
def test_giant_utf8_and_terminal_control_blocks_round_trip_both_directions(tmp_path, case, unit):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    block = report["walk"]["blocks"][-1]
    expected = (unit * 5000).encode("utf-8")
    assert block["bytes"] == len(expected)
    assert block["block_sha256"] == hashlib.sha256(expected).hexdigest()
    assert block["text_sha256"] == block["block_sha256"]
    assert report["walk"]["pages"] > 2
    assert len(report["requests"]) == 1


def test_rich_saved_activity_is_inert_deduplicated_and_has_no_embedded_image_bytes(tmp_path):
    report = launch(tmp_path, case="rich")
    assert_read_only_inspection(report)
    blocks = report["walk"]["blocks"]
    displayed = texts(report)
    assert "Hidden duplicate Codex fallback" not in displayed
    assert displayed.count("Codex shown answer") == 1
    assert "First card" in displayed and "Second card" in displayed
    assert "Saved reasoning only" in displayed
    assert "Saved external summary" in displayed and "Saved final recap" in displayed
    assert {"reasoning", "tool", "plan", "summary", "attachment", "notice"} <= {row["kind"] for row in blocks}
    assert sum(row["kind"] == "attachment" for row in blocks) == 2
    assert "NOT_REAL_IMAGE" not in json.dumps(blocks)
    assert "NEVER_EMIT_UNKNOWN_BASE64" not in json.dumps(blocks)
    assert "https://invalid.example" not in json.dumps(blocks)
    notices = [row["text"] for row in blocks if row["kind"] == "notice"]
    assert sum("Latest saved question" in (text or "") for text in notices) == 1
    assert "Stale saved question" not in json.dumps(blocks)
    assert "Terminal saved question" not in json.dumps(blocks)
    assert not report["interactive_events"]
    running = next(row for row in blocks if row["metadata"]["item_id"] == "tool-1")
    assert running["state"] == "incomplete"
    assert "No completion was saved" in running["text"]
    assert len(report["requests"]) == 1


def test_identical_saved_user_text_is_never_deduplicated_as_a_live_prompt(tmp_path):
    report = launch(tmp_path, case="repeated")
    assert_read_only_inspection(report)
    assert texts(report).count("identical prompt") == 2
    assert report["requests"][0]["users"] == ["identical prompt", "identical prompt",
                                              "Explicit prompt after saved history inspection"]


@pytest.mark.parametrize("case", ["mixed", "torn"])
def test_pages_follow_canonical_edit_truncate_rename_and_torn_record_replay(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    assert report["descriptor"]["title"] == "Canonical renamed history"
    assert texts(report) == ["Truncated canonical prefix", "Saved original answer", "Final canonical answer"]
    assert "Edited canonical prompt" not in texts(report)
    assert len(report["requests"]) == 1


def test_pending_snapshot_and_empty_page_do_not_materialize_before_explicit_prompt(tmp_path):
    report = launch(tmp_path, case="pending")
    assert_read_only_inspection(report)
    assert report["descriptor"]["pending"] and not report["descriptor"]["materialized"]
    assert report["descriptor"]["transcript_revision"] is None
    assert report["descriptor"]["fresh_system_context"]
    assert report["descriptor"]["counts"]["non_system"] == 0
    assert report["descriptor"]["counts"]["blocks"] == 0
    assert report["walk"]["blocks"] == []
    assert len(report["requests"]) == 1 and not report["after"]["pending"]


@pytest.mark.parametrize("case", ["stale", "cursor-tamper", "foreign", "policy-stale"])
def test_stale_foreign_or_tampered_page_refuses_without_new_side_effects(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    assert report["refusal"] and report["refusal_unchanged"]
    assert not report["requests"]


def test_compact_guarded_open_accepts_many_messages_then_verifies_new_current_context(tmp_path):
    report = launch(tmp_path, case="preview-open")
    assert_read_only_inspection(report)
    result = report["open"]
    assert result["status"] == "connected" and result["mutated"] and not result["close_required"]
    assert result["worker_settled"] and result["connection_settled"] and result["history_settled"]
    assert "messages" not in result
    assert report["after_open"]["session_id"] == report["target"]
    assert report["after_open"]["counts"]["messages"] >= 1203
    assert report["after_open"]["context_sha256"] == result["history"]["context_sha256"]
    assert len(report["requests"]) == 1 and report["requests"][0]["session_id"] == report["target"]


def test_target_edit_after_preview_refuses_open_and_preserves_original_connection(tmp_path):
    report = launch(tmp_path, case="open-stale")
    assert_read_only_inspection(report)
    assert report["open"]["status"] == "refused"
    assert not report["open"]["mutated"] and not report["open"]["close_required"]
    assert report["after"] == report["before"] and not report["requests"]


def test_negotiated_resume_rejects_oversized_raw_store_before_saved_widget_hydration(tmp_path):
    seed = launch(tmp_path, case="source-budget")
    assert seed["seed_only"] and not seed["requests"]
    resumed = launch(tmp_path, case="source-budget", phase="resume",
                     resume=seed["after"]["session_id"], allow_failure=True)
    assert resumed["returncode"] != 0 or resumed.get("ready", {}).get("launch_status") != "ready"
    assert not resumed["requests"] and not resumed["bubbles"]



@pytest.mark.parametrize("case", ["hidden-system", "hidden-provider"])
def test_hidden_context_changes_have_distinct_full_context_proof_with_same_visible_text(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    before, after = report["descriptor"], report["hidden_after"]
    assert before["context_sha256"] != after["context_sha256"]
    assert before["snapshot"] != after["snapshot"]
    assert before["counts"] == after["counts"]
    assert [row["text"] for row in report["walk"]["blocks"]] == [
        row["text"] for row in report["hidden_walk"]["blocks"]]
    assert not report["requests"]


def test_explicit_open_runs_only_reviewed_canonical_leave_resume_hooks(tmp_path):
    report = launch(tmp_path, case="hooks-success")
    assert_read_only_inspection(report)
    assert report["descriptor"]["open_effects"]["lifecycle_hooks"]
    assert report["open"]["status"] == "connected"
    hooks = [row["document"] for row in report["events_at_open"] if row["effect"] == "hook"]
    assert [row["event"] for row in hooks] == ["conversation_leave", "conversation_resume"]
    assert hooks[0]["conversation_id"] == report["before"]["session_id"]
    assert hooks[1]["conversation_id"] == report["target"]
    assert len(report["requests"]) == 1


@pytest.mark.parametrize("case", ["hooks-approval", "hooks-failed-leave", "hooks-failed-resume"])
def test_open_hook_refusal_or_failure_is_visible_and_never_autoapproved_or_prompted(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    assert report["open"]["status"] == "failed" and report["open"]["close_required"]
    assert report["open"]["error"] and not report["requests"]
    if case == "hooks-approval":
        assert not [row for row in report["events"] if row["effect"] == "hook"]


def test_hook_config_changed_after_preview_refuses_before_open_or_hook_execution(tmp_path):
    report = launch(tmp_path, case="hooks-drift")
    assert_read_only_inspection(report)
    assert report["open"]["status"] == "refused" and not report["open"]["mutated"]
    assert not report["open"]["close_required"]
    assert not report["events_at_open"] and not report["requests"]
    assert report["before"] == report["after"]


@pytest.mark.parametrize("case", ["quit-during-open", "eof-during-open"])
def test_shutdown_or_eof_joins_open_and_hook_process_before_releasing_ownership(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    assert not report["requests"]
    events = report["events"]
    assert any(row["effect"] == "hook-kill" for row in events)
    released = [row for row in events if row["effect"] == "lease-release"]
    assert released and all(not row["pending"] and not row["hook_alive"] for row in released)
    if case == "quit-during-open":
        ack, = [row for row in events if row["effect"] == "shutdown-ack"]
        assert ack["ok"] and not ack["pending"] and not ack["hook_alive"]


def test_readonly_owned_target_is_inspectable_but_open_refuses_without_effects(tmp_path):
    report = launch(tmp_path, case="preview-readonly")
    assert_read_only_inspection(report, read_only=True)
    assert report["open"]["status"] == "refused"
    assert not report["open"]["mutated"] and not report["open"]["close_required"]
    assert report["after"] == report["before"] and not report["requests"]


def test_inspection_preserves_actual_restored_held_input_payload_order_and_barrier(tmp_path):
    report = launch(tmp_path, case="held")
    assert_read_only_inspection(report)
    assert len(report["before"]["queue"]) == 1
    assert report["before"]["queue"] == report["after"]["queue"]
    assert report["before"] == report["after"] and not report["requests"]


def test_prior_process_cursor_refuses_after_owned_restart_then_fresh_inspection_allows_prompt(tmp_path):
    first = launch(tmp_path, case="restart-cursor")
    assert_read_only_inspection(first)
    assert not first["requests"]
    resumed = launch(tmp_path, case="restart-cursor", phase="resume", resume=first["after"]["session_id"])
    assert_read_only_inspection(resumed)
    assert resumed["restart_cursor_refusal"]
    assert first["descriptor"]["snapshot"] != resumed["descriptor"]["snapshot"]
    assert len(resumed["requests"]) == 1


def test_codex_saved_inspection_never_requests_provider_history_or_resumes_a_turn(tmp_path):
    report = launch(tmp_path, case="codex-inspect")
    assert_read_only_inspection(report)
    assert all(row["method"] in {"account/read", "model/list"} for row in report["provider_calls_at_inspection"])
    assert len(report["provider_requests"]) == 1
    assert report["provider_requests"][0]["thread_id"] == "history-native-thread"
    assert all(row["explicit"] for row in report["provider_calls"] if row["method"] in {"thread/resume", "turn/start"})


def test_open_waits_for_real_codex_history_adapter_reconciliation_before_returning_new_descriptor(tmp_path):
    report = launch(tmp_path, case="codex-open")
    assert_read_only_inspection(report)
    assert report["open"]["status"] == "connected"
    assert report["after_open"]["context_sha256"] != report["descriptor"]["context_sha256"]
    assert report["open"]["history"]["context_sha256"] == report["after_open"]["context_sha256"]
    calls = report["provider_calls_after_open"]
    assert sum(row["method"] == "thread/read" for row in calls) == 1
    assert sum(row["method"] == "thread/turns/list" for row in calls) == 2
    assert not any(row["method"] in {"thread/start", "thread/resume", "turn/start"} for row in calls)
    shown = [row["text"] for row in report["after_open_walk"]["blocks"]]
    assert "Hydrated saved answer" in shown
    assert any(text and "Hydrated saved tool result" in text for text in shown)
    assert len(report["provider_requests"]) == 1


def test_native_history_reconciliation_failure_prevents_false_connected_open(tmp_path):
    report = launch(tmp_path, case="codex-open-history-error")
    assert_read_only_inspection(report)
    assert report["open"]["status"] == "failed" and report["open"]["close_required"]
    assert report["open"]["error"] and not report["provider_requests"]


@pytest.mark.parametrize("case", ["codex-quit-during-hydration", "codex-eof-during-hydration",
                                  "codex-quit-during-connect", "codex-eof-during-connect"])
def test_shutdown_or_eof_keeps_ownership_until_captured_provider_worker_settles(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_read_only_inspection(report)
    result = report["open"]
    assert result["status"] == "failed" and result["close_required"] and result["worker_settled"]
    assert not report["requests"] and not report["provider_requests"]
    effects = [row["effect"] for row in report["events"]]
    assert effects.index("provider-entered") < effects.index("provider-shutdown-held")
    assert effects.index("provider-shutdown-held") < effects.index("provider-settled")
    assert effects.index("provider-settled") < effects.index("lease-release")
    released = [row for row in report["events"] if row["effect"] == "lease-release"]
    assert released and all(not row["pending"] and not row["provider_active"] for row in released)
    assert not any(row["method"] in {"thread/start", "thread/resume", "turn/start"} for row in report["provider_calls"])
    assert report["worker_settlement"] and all(row["finished"] for row in report["worker_settlement"])
    assert any(row["group"] == "ctx" for row in report["worker_settlement"])
    if case.endswith("-connect"):
        assert not any(row["method"] == "thread/read" for row in report["provider_calls"])
    if case.startswith("codex-quit-"):
        ack, = [row for row in report["events"] if row["effect"] == "shutdown-ack"]
        assert ack["ok"] and not ack["pending"] and not ack["provider_active"]


@pytest.mark.parametrize("case", ["lmstudio-inspect", "lmstudio-open"])
def test_lmstudio_history_startup_resume_and_open_never_launch_implicit_thinking_probe(tmp_path, case):
    first = launch(tmp_path, case=case)
    for report in (first, launch(tmp_path, case=case, phase="resume", resume=first["after"]["session_id"])):
        assert_read_only_inspection(report)
        assert "--thinking-level" not in report["launch_arguments"]
        assert not report["startup_bubbles"]
        assert report["provider_calls"] and all(row["method"] == "models/read" for row in report["provider_calls"])
        assert not any(row["group"] == "thinking-probe" for row in report["worker_settlement"])
        assert len(report["requests"]) == 1
        assert report["requests"][0]["backend"] == "lmstudio"
    if case == "lmstudio-open":
        assert first["thinking_choice_retired_launch"]
        assert first["open"]["status"] == "connected"
        assert first["after_open"]["session_id"] == first["target"]
