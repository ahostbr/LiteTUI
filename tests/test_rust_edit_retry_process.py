"""Reviewed edit/retry through actual owned RPC, storage and SDK boundaries."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/history_edit_retry_runtime.py"


def launch(tmp_path, case):
    data = tmp_path / "data"
    data.mkdir()
    settings = {"backend": "custom", "default_model": "switch-old", "backend_chosen": True,
                "thinking_level": "default", "user_name_asked": True, "tools_enabled": False,
                "mcp_enabled": False, "skills_enabled": False, "footer_task_manager": False,
                "tts_enabled": False, "sidecar_enabled": False, "tool_policy_profile": "strict",
                "autocompact_enabled": False, "tool_auto_background_s": 0,
                "custom_base_url": "http://127.0.0.1:45120/v1", "custom_context_length": 1000000,
                "max_tokens_chat": 101, "max_tokens_tools": 202, "theme_name": "textual-dark", "custom_themes": {}}
    question = "question-" in case
    aftercare = question and case.endswith("compaction") or case in {"auto-compact", "self-compact", "quit-compaction", "eof-compaction", "stop-compaction"}
    backend = "lmstudio" if case == "self-compact" or question else "custom"
    if aftercare:
        settings.update(backend=backend, tools_enabled=case == "self-compact" or question, compact_keep_recent=2,
            autocompact_enabled=case != "self-compact", autocompact_at_percent=80, wake_after_compact=True)
    if question:
        settings.update(backend=backend, tools_enabled=True)
    (data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE=("lmstudio-rewrite-" if backend == "lmstudio" else "rewrite-") + case,
                       RUST_HISTORY_PHASE="fresh", RUST_REWRITE_CASE=case, RUST_REWRITE_DRIVER="self",
                       LITETUI_DATA_ROOT=str(data), PYTHONPATH=str(ROOT / "tests"),
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    argv = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input", "--history-projection-version", "1",
            "--backend", backend, "--model", "switch-old", "--tool-profile", "strict", "--thinking-level", "default",
            "--mcp-servers", "none", "--server-mode", "connect", "--server-timeout", "5",
            "--base-url", "http://127.0.0.1:45120/v1"]
    output, errors = tmp_path / "events.jsonl", tmp_path / "stderr.txt"
    with output.open("wb") as stdout, errors.open("wb") as stderr:
        process = subprocess.Popen(argv, cwd=tmp_path, env=environment, stdout=stdout, stderr=stderr,
                                   stdin=subprocess.PIPE if case.startswith("eof-") else subprocess.DEVNULL)
        try:
            if case.startswith("eof-"):
                marker = data / "rewrite-stage.json"
                deadline = time.monotonic() + 30
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert marker.exists(), output.read_text(encoding="utf-8") + errors.read_text(encoding="utf-8")
                assert json.loads(marker.read_text(encoding="utf-8"))["stage"] == case.removeprefix("eof-")
                assert process.stdin is not None
                process.stdin.close()  # Actual OS pipe EOF reaches rpc._reader_loop.
            process.wait(timeout=130)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert process.returncode == 0, output.read_text(encoding="utf-8") + errors.read_text(encoding="utf-8")
    report = json.loads((data / "history-fresh-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == source / "src/litetui/app.py"
    assert report["denied"] == []
    assert report["provider_requests"] == []
    if question:
        marker = json.loads((data / "rewrite-stage.json").read_text(encoding="utf-8"))
        assert marker == {"stage": "question-compaction" if aftercare else "question-stream",
            "ask_id": report["question_request"]["event"]["id"],
            "execution_id": report["question_request"]["execution_id"]}
    if backend == "lmstudio":
        assert report["provider_calls"] and all(row["method"] == "models/read" for row in report["provider_calls"])
    else:
        assert report["provider_calls"] == []
    assert len(report["sdk_calls"]) == len(report["all_sdk_requests"])
    if not aftercare:
        assert len(report["sdk_calls"]) == len(report["requests"])
        assert all(call["stream"] for call in report["sdk_calls"])
    assert all(worker["finished"] for worker in report["worker_settlement"] + report["rewrite_workers"])
    assert all(process["returncode"] is not None for process in report["hook_processes"])
    assert all(row["wire_bytes"] <= 1024 * 1024 for row in report["observations"])
    forbidden = {"gui.conversations.edit", "gui.conversations.retry", "gui.conversations.native_open", "prompt"}
    assert not any(command["type"] in forbidden for command in report["commands"])
    return report


def assert_unchanged(report, before="before"):
    assert report["after"] == report[before]
    baseline = report.get("baseline_live", report["original"])
    assert report["live"] == baseline
    assert report["durable"] == baseline
    assert report["requests"] == []


@pytest.mark.parametrize("case", ["edit", "retry", "unicode", "text-boundary", "replayed-apply", "hooks-allow"])
def test_reviewed_edit_retry_persists_one_complete_candidate_and_sends_only_exact_prefix(tmp_path, case):
    report = launch(tmp_path, case)
    receipt = report["apply"]
    assert receipt["turn"] == "accepted" and receipt["mutated"] and not receipt["close_required"]
    assert receipt["persistence"] == {"snapshot_written": True, "readback_verified": True}
    assert report["settled"]["worker_settled"] and not report["settled"]["close_required"]
    assert report["durable"] == report["live"]
    assert report["snapshots_after"][:-1] == report["snapshots_before"]
    assert report["snapshots_after"][-1]["messages"] == report["candidate"]
    assert report["snapshots_after"][-1]["store_identity"] == report["before"]["store_identity"]
    assert report["snapshots_after"][-1]["store_injected"] == report["before"]["store_injected"]
    assert report["live"][:len(report["candidate"])] == report["candidate"]
    assert len(report["requests"]) == 1
    request = report["requests"][0]
    assert request["complete_prepared_context"]
    assert request["wire_row_hashes"] == request["expected_row_hashes"]
    assert request["context_sha256"] == request["prepared_sha256"]
    assert request["wire_roles"] == [row["role"] for row in report["prefix"]] + ["user"]
    assert request["last"] == {"role": "user", "content": report["replacement"]}
    assert request["session_id"] == report["before"]["identity"]["session_id"]
    assert request["agent_id"] == report["before"]["identity"]["agent_id"]
    assert request["model"] == "switch-old" and request["backend"] == "custom"
    for field in ("settings", "authority", "handles", "store_identity", "store_injected"):
        assert report["after"][field] == report["before"][field]
    events = report["wire_events"]
    assert sum(row.get("type") == "turn_start" for row in events) == 1
    assert sum(row.get("type") == "turn_end" for row in events) == 1
    envelopes = [row for row in events if row.get("type") == "gui.event" and row["event"]["type"] in {"turn_start", "turn_end"}]
    assert len(envelopes) == 2 and all(row["operation_id"] == receipt["operation_id"] for row in envelopes)
    assert envelopes[0]["execution_id"] == envelopes[1]["execution_id"]
    assert envelopes[0]["execution_id"] and all(row["source"] == "rpc" for row in envelopes)
    if case == "hooks-allow":
        assert [row["event"] for row in report["hook_documents"]] == ["prompt_before"]
    if case == "text-boundary":
        assert len(report["replacement"].encode("utf-8")) == 65536


@pytest.mark.parametrize("case", ["over-budget", "slash", "image-path", "empty", "text-over-limit",
    "non-user", "multimodal-target", "stale-message", "foreign-session", "pending-image", "held",
    "orphan-tool", "missing-tool-id", "readonly"])
def test_preflight_and_selection_refusals_preserve_exact_context_and_disk(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["refused_at"] in {"snapshot", "eligibility", "validate"}
    assert_unchanged(report)
    assert report["hook_documents"] == []


@pytest.mark.parametrize("case", ["hooks-deny", "hooks-approval", "stale-settings", "stale-hook-config", "stale-history", "hooks-drift", "cancel-admission"])
def test_prepublication_refusals_do_not_truncate_or_execute(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["apply"]["turn"] == "refused"
    assert not report["apply"]["mutated"] and not report["apply"]["close_required"]
    assert report["apply"]["refusal_history"] == report["settled"]["history"] == report["fresh"]
    assert report["apply"]["persistence"] == {"snapshot_written": False, "readback_verified": False}
    assert_unchanged(report, "before_apply")
    if case == "hooks-approval":
        assert report["hook_documents"] == report["interactive_events"] == []


@pytest.mark.parametrize("case", ["wrong-echo", "bool-version", "float-index", "superseded-review"])
def test_changed_review_echo_is_a_protocol_refusal_without_effects(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["apply_response"]["ok"] is False
    assert "echo" in report["apply_response"]["error"]
    assert_unchanged(report, "before_apply")


@pytest.mark.parametrize("case", ["persist-silent", "persist-torn", "persist-wrong", "persist-receipts"])
def test_uncertain_snapshot_publication_closes_without_provider_execution_or_rollback(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["apply"]["turn"] == "failed" and report["apply"]["close_required"]
    assert not report["apply"]["persistence"]["readback_verified"]
    assert report["requests"] == [] and report["snapshot_attempts"] == 1
    assert report["live"] == report["original"]


def test_failure_after_verified_publication_retains_candidate_and_closes_without_rollback(tmp_path):
    report = launch(tmp_path, "schedule-fail")
    assert report["apply"]["turn"] == "failed" and report["apply"]["close_required"]
    assert report["apply"]["mutated"]
    assert report["apply"]["persistence"] == {"snapshot_written": True, "readback_verified": True}
    assert report["live"] == report["durable"] == report["candidate"]
    assert report["snapshots_after"][:-1] == report["snapshots_before"]
    assert report["snapshots_after"][-1]["messages"] == report["candidate"]
    assert report["requests"] == [] and report["blocked_after_failure"]["ok"] is False


@pytest.mark.parametrize("case", ["quit-admission", "eof-admission", "quit-worker", "eof-worker"])
def test_quit_and_real_stdin_eof_join_exact_admission_or_stream_before_lease_release(tmp_path, case):
    report = launch(tmp_path, case)
    shutdown = report["shutdown"]
    assert shutdown["worker_settled"] and shutdown["admission_settled"] and shutdown["hooks_settled"]
    assert shutdown["effects_verified"] is False and shutdown["pending_at_close"]
    assert shutdown["session_id"] == shutdown["current_session_id"] == report["before"]["identity"]["session_id"]
    assert shutdown["agent_id"] == report["before"]["identity"]["agent_id"]
    assert shutdown["backend"] == "custom" and shutdown["model"] == "switch-old"
    assert shutdown["action_id"] == report["review"]["action_id"] and shutdown["action"] == "edit"
    assert shutdown["operation_id"] == report["apply_response"]["id"] == report["apply"]["operation_id"]
    assert report["durable"] == report["live"]
    released = [row for row in report["events"] if row["effect"] == "rewrite-lease-release"]
    assert released and all(not row["active_chat"] and not row["active_hooks"] for row in released)
    if case.endswith("admission"):
        assert report["live"] == report["original"] and report["requests"] == []
        assert shutdown["mutated"] is False
    else:
        assert len(report["requests"]) == 1 and report["requests"][0]["complete_prepared_context"]
        assert shutdown["mutated"] is True
        assert report["live"][:len(report["candidate"])] == report["candidate"]


@pytest.mark.parametrize("case", ["auto-compact", "self-compact"])
def test_canonical_compaction_and_wake_preserve_complete_ordered_sdk_context_and_execution_chain(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["apply"]["turn"] == "accepted" and not report["apply"]["close_required"]
    assert report["durable"] == report["live"]
    calls = report["all_sdk_requests"]
    expected_kinds = ["self-tool", "card-summary", "compaction", "wake"] if case == "self-compact" else ["turn", "compaction", "wake"]
    assert [row["kind"] for row in calls] == expected_kinds
    first = calls[0]["messages"]
    assert first == report["initial_prepared"]
    assert first[1:] == report["candidate"][1:]
    assert report["prefix"][0]["content"] in first[0]["content"]
    assert next(row["messages"] for row in calls if row["kind"] == "compaction") == report["expected_compaction_request"]
    assert calls[-1]["messages"] == report["expected_wake_request"]
    for row in calls:
        assert row["agent_id"] == report["before"]["identity"]["agent_id"]
        assert row["session_id"] == report["before"]["identity"]["session_id"]
        assert row["model"] == "switch-old" and row["backend"] == ("lmstudio" if case == "self-compact" else "custom")
        assert all("DISCARDED" not in str(message) for message in row["messages"])
    assert len(report["requests"]) == 2 and all(row["complete_prepared_context"] for row in report["requests"])
    events = [row for row in report["wire_events"] if row.get("type") == "gui.event"]
    starts = [row for row in events if row["event"]["type"] == "turn_start"]
    ends = [row for row in events if row["event"]["type"] == "turn_end"]
    assert len(starts) == len(ends) == 2
    assert [row["execution_id"] for row in starts] == [row["execution_id"] for row in ends]
    assert len({row["execution_id"] for row in starts}) == 2
    previous = None
    for index, row in enumerate(starts):
        assert row["event"]["rewrite_segment"] == {"action_id": report["review"]["action_id"],
            "index": index, "previous_execution_id": previous}
        assert row["operation_id"] == report["apply"]["operation_id"] and row["source"] == "rpc"
        previous = row["execution_id"]
    maintenance = [row for row in events if row["event"]["type"] == "rewrite_aftercare"]
    assert len(maintenance) == 2 and [row["event"]["phase"] for row in maintenance] == ["start", "end"]
    assert maintenance[0]["execution_id"] == maintenance[1]["execution_id"]
    for row in maintenance:
        assert row["event"]["kind"] == "compaction"
        assert row["event"]["action_id"] == report["review"]["action_id"]
        assert row["event"]["after_execution_id"] == starts[0]["execution_id"]
        assert row["event"]["aftercare_id"] == row["execution_id"]
        assert row["operation_id"] == report["apply"]["operation_id"] and row["source"] == "rpc"
    assert report["settled"]["execution"] == {"segments": 2, "last_execution_id": starts[-1]["execution_id"]}
    assert report["settled"]["history"] == report["fresh"]
    compacted = [row for row in report["wire_events"] if row.get("type") == "compaction" and row.get("reason") == "compacted"]
    assert len(compacted) == 1


@pytest.mark.parametrize("case", ["quit-compaction", "eof-compaction", "stop-compaction"])
def test_shutdown_or_stop_during_owned_compaction_joins_worker_and_never_wakes(tmp_path, case):
    report = launch(tmp_path, case)
    assert [row["kind"] for row in report["all_sdk_requests"]] == ["turn", "compaction"]
    assert report["durable"] == report["live"] == report["before_compaction"]
    assert sum(row.get("type") == "turn_start" for row in report["wire_events"]) == 1
    if case == "stop-compaction":
        assert report["settled"]["worker_settled"] and report["settled"]["execution"]["segments"] == 1
    else:
        proof = report["shutdown"]
        assert proof["worker_settled"] and proof["admission_settled"] and proof["hooks_settled"]
        assert proof["pending_at_close"] and proof["mutated"] and not proof["effects_verified"]
        assert proof["operation_id"] == report["apply_response"]["id"]
        assert proof["action_id"] == report["review"]["action_id"]
        assert proof["session_id"] == report["before"]["identity"]["session_id"]
    release = [row for row in report["events"] if row["effect"] == "rewrite-lease-release"]
    assert release and all(not row["active_chat"] and not row["active_hooks"] for row in release)


def assert_question_scope_and_retirement(report, *, compaction, cancelled):
    asked = report["question_request"]
    events = [row for row in report["wire_events"] if row.get("type") == "gui.event"]
    requests = [row for row in events if row["event"]["type"] == "user_input_requested"]
    assert requests == [asked]
    assert asked["operation_id"] == report["apply"]["operation_id"] and asked["source"] == "rpc"
    assert asked["session_id"] == report["before"]["identity"]["session_id"]
    parent_type = "rewrite_aftercare" if compaction else "turn_start"
    owner = next(row for row in events if row["event"]["type"] == parent_type)
    assert asked["execution_id"] == owner["execution_id"]
    assert asked["event"]["questions"][0]["question"] == "Which continuation is authorized?"
    assert asked["event"]["questions"][0]["multiSelect"] is False
    assert len(report["question_threads"]) == len(report["question_futures"]) == 1
    thread, future = report["question_threads"][0], report["question_futures"][0]
    assert thread["retired"] and future["done"] and not future["cancelled"]
    assert future["producer_done"] and future["callback_retired"]
    assert future["lifetime_cancelled"] is cancelled
    assert thread["origin"] == future["origin"]
    for field in ("operation_id", "execution_id", "session_id", "source", "hook_id"):
        assert thread["origin"][field] == asked[field]
    assert report["question_pending"] == []
    released = [row for row in report["events"] if row["effect"] == "rewrite-lease-release"]
    assert released and all(not row["active_chat"] and not row["active_hooks"] for row in released)
    if cancelled:
        assert "ABORTED" in thread["result"] and "SUBMITTED" not in thread["result"]
        resolved = [row for row in events if row["event"]["type"] == "user_input_resolved"]
        assert len(resolved) == 1 and resolved[0]["event"]["id"] == asked["event"]["id"]
        assert resolved[0]["event"]["cancelled"] is True
        for field in ("operation_id", "execution_id", "session_id", "source", "hook_id"):
            assert resolved[0][field] == asked[field]
    else:
        assert "SUBMITTED — 1 of 1 answered" in thread["result"]
        assert "Explicit controlled answer 雪" in thread["result"]
        assert report["question_answer"]["result"]["answered"] == asked["event"]["id"]


@pytest.mark.parametrize("case", ["question-stream", "question-compaction"])
def test_real_question_thread_keeps_exact_execution_origin_and_answer_in_complete_next_request(tmp_path, case):
    report = launch(tmp_path, case)
    compaction = case.endswith("compaction")
    assert_question_scope_and_retirement(report, compaction=compaction, cancelled=False)
    terminals = [row for row in report["wire_events"] if row.get("type") == "turn_end"]
    assert terminals and all(row["stopReason"] == "stop" for row in terminals)
    assert report["apply"]["turn"] == "accepted" and not report["apply"]["close_required"]
    assert report["settled"]["worker_settled"] and report["settled"]["history"] == report["fresh"]
    assert report["durable"] == report["live"]
    calls = report["all_sdk_requests"]
    kinds = (["turn", "compaction-question", "compaction-followup", "wake"] if compaction
             else ["question-stream", "question-followup"])
    assert [row["kind"] for row in calls] == kinds
    assert calls[0]["messages"] == report["initial_prepared"]
    followup = next(row for row in calls if row["kind"].endswith("followup"))
    assert followup["messages"] == report["expected_question_followup"]
    assert followup["messages"][-1]["content"] == report["question_threads"][0]["result"]
    assert all(row["complete_prepared_context"] for row in report["requests"])
    assert len(report["requests"]) == 2
    assert report["settled"]["execution"]["segments"] == (2 if compaction else 1)
    if compaction:
        assert calls[1]["messages"] == report["expected_compaction_request"]
        assert calls[-1]["messages"] == report["expected_wake_request"]
    else:
        assert report["live"][:len(report["candidate"])] == report["candidate"]


@pytest.mark.parametrize("case", ["quit-question-stream", "eof-question-stream",
                                 "quit-question-compaction", "eof-question-compaction"])
def test_quit_and_real_eof_retire_waiting_question_callback_before_owned_store_release(tmp_path, case):
    report = launch(tmp_path, case)
    compaction = case.endswith("compaction")
    assert_question_scope_and_retirement(report, compaction=compaction, cancelled=True)
    proof = report["shutdown"]
    assert proof["worker_settled"] and proof["admission_settled"] and proof["hooks_settled"]
    assert proof["pending_at_close"] and proof["mutated"] and not proof["effects_verified"]
    assert proof["operation_id"] == report["apply_response"]["id"] == report["apply"]["operation_id"]
    assert proof["action_id"] == report["review"]["action_id"]
    assert proof["session_id"] == report["before"]["identity"]["session_id"]
    assert report["durable"] == report["live"]
    assert [row["kind"] for row in report["all_sdk_requests"]] == (
        ["turn", "compaction-question"] if compaction else ["question-stream"])
    assert len(report["requests"]) == 1 and report["requests"][0]["complete_prepared_context"]
    assert not any(command["type"] == "answer" for command in report["commands"])
    if compaction:
        assert report["live"] == report["before_compaction"]
    else:
        assert report["live"][:len(report["candidate"])] == report["candidate"]


@pytest.mark.parametrize("case", ["stop-question-stream", "stop-question-compaction"])
def test_soft_stop_cancels_the_exact_question_and_settles_without_followup_or_wake(tmp_path, case):
    report = launch(tmp_path, case)
    compaction = case.endswith("compaction")
    assert_question_scope_and_retirement(report, compaction=compaction, cancelled=True)
    assert report["apply"]["turn"] == "accepted" and not report["apply"]["close_required"]
    assert report["settled"]["worker_settled"] and not report["settled"]["close_required"]
    assert report["settled"]["history"] == report["fresh"]
    assert report["settled"]["execution"]["segments"] == 1
    assert report["durable"] == report["live"]
    assert report["question_stop"]["result"]["stopped"] is True
    assert report["question_stop"]["result"]["cancelled_asks"] == [report["question_request"]["event"]["id"]]
    commands = report["commands"]
    assert sum(row["type"] == "abort" for row in commands) == 1
    assert not any(row["type"] in {"answer", "shutdown"} for row in commands)
    assert [row["kind"] for row in report["all_sdk_requests"]] == (
        ["turn", "compaction-question"] if compaction else ["question-stream"])
    assert len(report["requests"]) == 1 and report["requests"][0]["complete_prepared_context"]
    terminals = [row for row in report["wire_events"] if row.get("type") == "turn_end"]
    assert len(terminals) == 1 and terminals[0]["stopReason"] == ("stop" if compaction else "cancelled")
    if compaction:
        assert report["live"] == report["before_compaction"]
        notices = [row for row in report["wire_events"] if row.get("type") == "compaction"]
        assert len(notices) == 1 and notices[0]["reason"] == "cancelled"
    else:
        assert report["live"][:len(report["candidate"])] == report["candidate"]
        assert len(report["live"]) == len(report["candidate"]) + 2
        assert report["live"][-2]["tool_calls"][0]["id"] == "rewrite-question-call"
        assert report["live"][-1] == {"role": "tool", "tool_call_id": "rewrite-question-call",
            "name": "ask_user_question", "content": report["question_threads"][0]["result"]}
