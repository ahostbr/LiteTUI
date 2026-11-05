"""Reviewed manual compaction through actual owned RPC and canonical persistence."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/history_compact_runtime.py"
SUMMARY = "Controlled compacted continuity summary"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def launch(tmp_path, case):
    data = tmp_path / "data"
    data.mkdir()
    tools = case in {"tools-on", "tool-then-empty", "tool-cap"} or "question" in case or "approval" in case
    wake = case == "wake" or case.endswith("-wake")
    native = case.startswith("codex-native")
    claude = case.startswith("claude-segment")
    backend, model = ("claude", "claude-fixture") if claude else ("codex", "gpt-6.1-sol") if native else ("custom", "switch-old")
    settings = {"backend": backend, "default_model": model, "backend_chosen": True, "codex_native_engine": native,
        "thinking_level": "default", "user_name_asked": True, "tools_enabled": tools,
        "mcp_enabled": False, "skills_enabled": False, "footer_task_manager": False,
        "tts_enabled": False, "sidecar_enabled": False, "tool_policy_profile": "strict",
        "autocompact_enabled": False, "tool_auto_background_s": 0,
        "custom_base_url": "http://127.0.0.1:45120/v1", "custom_context_length": 1000000,
        "max_tokens_chat": 101, "max_tokens_tools": 202, "theme_name": "textual-dark", "custom_themes": {},
        "compact_keep_recent": 2, "compact_max_tokens": 256, "compact_max_tool_iters": 2,
        "compact_thinking_level": "off", "wake_after_compact": wake,
        "clear_screen_after_compact": case == "clear-screen"}
    (data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="compact-" + case,
        RUST_HISTORY_PHASE="fresh", RUST_COMPACT_CASE=case, RUST_COMPACT_DRIVER="self",
        LITETUI_DATA_ROOT=str(data), PYTHONPATH=str(ROOT / "tests"),
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    argv = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input", "--history-projection-version", "1",
        "--backend", backend, "--model", model, "--tool-profile", "strict", "--thinking-level", "default",
        "--mcp-servers", "none"]
    argv.extend([] if claude else ["--codex-engine", "native"] if native else [
        "--server-mode", "connect", "--server-timeout", "5", "--base-url", "http://127.0.0.1:45120/v1"])
    output, errors = tmp_path / "events.jsonl", tmp_path / "stderr.txt"
    with output.open("wb") as stdout, errors.open("wb") as stderr:
        process = subprocess.Popen(argv, cwd=tmp_path, env=environment, stdout=stdout, stderr=stderr,
            stdin=subprocess.PIPE if case.startswith("eof-") else subprocess.DEVNULL)
        try:
            if case.startswith("eof-"):
                marker = data / "compact-stage.json"
                deadline = time.monotonic() + 35
                expected_stage = "question" if "question" in case else "approval" if "approval" in case else "readiness" if "readiness" in case else "root-maintenance"
                reached = False
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        reached = marker.exists() and json.loads(marker.read_text(encoding="utf-8"))["stage"] == expected_stage
                    except (ValueError, OSError):
                        reached = False
                    if reached:
                        break
                    time.sleep(0.01)
                assert reached, output.read_text(encoding="utf-8") + errors.read_text(encoding="utf-8")
                assert process.stdin is not None
                process.stdin.close()
            process.wait(timeout=130)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert process.returncode == 0, f"Actual runtime failed; evidence: {tmp_path}\n" + errors.read_text(encoding="utf-8")[-8000:]
    report = json.loads((data / "history-fresh-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == source / "src/litetui/app.py"
    assert report["denied"] == report["provider_requests"] == []
    if native:
        assert all(call["method"] in {"account/read", "model/list"} for call in report["provider_calls"])
    else:
        assert report["provider_calls"] == []
    assert isinstance(report["sdk_requests"], list)
    assert all(w["finished"] for w in report["worker_settlement"] + report["compact_workers"])
    assert all(s["closed"] for s in report["streams"])
    assert all(t["retired"] for t in report["question_threads"])
    assert all(t["done"] and t["producer_done"] and t["callback_retired"] for t in report["tool_futures"])
    assert report["question_pending"] == report["approval_pending"] == report["approval_authorities"] == []
    assert report["archive_prefix_preserved"]
    assert all(row["wire_bytes"] <= 1024 * 1024 for row in report["observations"])
    if not case.startswith("legacy-"):
        assert not any(c["type"] in {"gui.conversations.compact", "gui.conversations.edit",
            "gui.conversations.retry", "prompt", "run_command"} for c in report["commands"])
    return report


@pytest.mark.parametrize("case", ["codex-native", "codex-native-drift", "codex-native-uncertain"])
def test_reviewed_codex_compaction_requires_persisted_native_identity(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["records_before"] == report["records_after"]
    assert report["live"] == report["baseline_live"]
    assert report["review"]["hint"] == "" and report["review"]["request_prompt_sha256"] is None
    if case == "codex-native-drift":
        assert report["apply"]["status"] == "refused" and not report.get("compaction_rpc")
        assert report["settled"]["witness"]["native_compaction"] is None
        return
    assert report["apply"]["status"] == "accepted"
    assert [call["method"] for call in report["compaction_rpc"]] == [
        "thread/read", "thread/compact/start", "thread/read", "thread/turns/list"]
    if case == "codex-native-uncertain":
        assert not report["settled_response"]["ok"] and "settled" not in report
        return
    assert report["settled"]["witness"]["native_compaction"] == {
        "thread_id": "history-native-thread", "turn_id": "native-compact-turn", "item_id": "native-compact-item"}
    assert not report["settled"]["witness"]["publication_attempted"]
    assert not report["settled"]["history_changed"]


@pytest.mark.parametrize("case", ["claude-segment", "claude-segment-foreign", "claude-segment-close",
    "claude-segment-ledger", "claude-segment-transcript", "claude-segment-wake", "claude-segment-tools",
    "claude-segment-drift"])
def test_claude_segment_and_transcript_publications_have_independent_evidence(tmp_path, case):
    report = launch(tmp_path, case)
    old, final = report["old_ledger"], report["final_ledger"]
    if case.endswith(("-wake", "-tools")):
        assert not report["snapshot"]["eligible"] and not report.get("claude_summary_prompts")
        return
    if case == "claude-segment-drift":
        assert report["apply"]["status"] == "refused" and not report.get("claude_summary_prompts")
        assert report["settled"]["witness"]["claude_compaction"] is None and final == old
        return
    assert len(report["claude_summary_prompts"]) == 1
    assert report["review"]["request_prompt_sha256"] == hashlib.sha256(report["claude_summary_prompts"][0].encode()).hexdigest()
    assert report["apply"]["status"] == "accepted"
    if case != "claude-segment":
        assert not report["settled_response"]["ok"] and "settled" not in report
        assert report["records_before"] == report["records_after"]
        stage = {"claude-segment-foreign": "SDK summary request", "claude-segment-close": "SDK session retirement",
                 "claude-segment-ledger": "replacement segment selection",
                 "claude-segment-transcript": "transcript publication after verified segment selection"}[case]
        assert report["settled_response"]["error"] == f"Claude compaction stopped during {stage}. Close and inspect the conversation."
        if case == "claude-segment-transcript":
            assert final["selected_segment"] != old["selected_segment"]
            assert final["segments"][final["selected_segment"]]["seed"] == SUMMARY
        else:
            assert final == old
        return
    witness = report["settled"]["witness"]["claude_compaction"]
    assert witness["result_id"] == report["claude_result"] and witness["native_session"] == report["claude_session"]
    assert witness["old_segment"] == old["selected_segment"] and witness["new_segment"] == final["selected_segment"]
    assert witness["ledger_before"] == digest(old) and witness["ledger_after"] == digest(final)
    selected = final["segments"][witness["new_segment"]]
    assert selected["seed"] == SUMMARY and selected["session_id"] is None
    assert witness["seed_sha256"] == hashlib.sha256(SUMMARY.encode()).hexdigest()
    assert report["settled"]["witness"]["publication_verified"]
    assert report["live"][-2]["content"].endswith("\n\n" + SUMMARY)


def root_events(report):
    return [r for r in report["wire_events"] if r.get("type") == "gui.event"
        and r["event"].get("type") == "compact_maintenance" and r["event"].get("root") is True]


def assert_input_scope(report, *, cancelled=False):
    asked = report["input_request"]
    kind = asked["event"]["type"]
    events = [r for r in report["wire_events"] if r.get("type") == "gui.event"]
    assert [r for r in events if r["event"].get("type") == kind] == [asked]
    assert asked["operation_id"] == report["apply"]["operation_id"] and asked["source"] == "rpc"
    assert asked["session_id"] == report["before"]["identity"]["session_id"]
    parent = next(r for r in events if r["execution_id"] == asked["execution_id"] and
        r["event"].get("type") in {"turn_start", "compact_maintenance"})
    assert parent["event"].get("root") is True or parent["event"].get("compact_segment")
    if kind == "user_input_requested":
        assert len(report["question_threads"]) == len(report["tool_futures"]) == 1
        thread, future = report["question_threads"][0], report["tool_futures"][0]
        assert thread["origin"] == future["origin"]
        assert future["lifetime_cancelled"] is cancelled
        for key in ("operation_id", "execution_id", "session_id", "source", "hook_id"):
            assert thread["origin"][key] == asked[key]
        if cancelled:
            closed = [r for r in events if r["event"].get("type") == "user_input_resolved"]
            assert len(closed) == 1
            assert {key: closed[0]["event"][key] for key in ("type", "id", "cancelled")} == {"type": "user_input_resolved", "id": asked["event"]["id"], "cancelled": True}
            assert closed[0]["event"].get("reason") in {None, "abort"}
            for key in ("operation_id", "execution_id", "session_id", "source", "hook_id"):
                assert closed[0][key] == asked[key]
        else:
            assert "Explicit controlled compaction answer 雪" in thread["result"]
            followup = report["expected_question_followup"]
            assert followup[-1]["content"] == thread["result"]


def assert_settled(report, outcome, reason_code):
    result = report["settled"]
    assert result["worker_settled"] is True and result["close_required"] is False
    assert result["outcome"] == outcome and result["reason_code"] == reason_code
    assert report["live"] == report["durable"]
    assert result["history"] == report["fresh"]
    assert result["history_changed"] == (digest(report["live"]) != digest(report["original"]))
    witness = result["witness"]
    assert witness["input_context_sha256"] == digest(report["original"])
    if report["apply"]["status"] == "accepted":
        assert result["status"] == "accepted" and witness["root_started"]
        assert witness["root_context_sha256"] == result["root_history"]["context_sha256"]
        markers = root_events(report)
        assert [r["event"]["phase"] for r in markers] == ["start", "end"]
        for row in markers:
            assert row["operation_id"] == report["apply"]["operation_id"]
            assert row["execution_id"] == report["review"]["root_execution_id"] == row["event"]["aftercare_id"]
            assert row["event"]["action_id"] == report["review"]["action_id"]
            assert row["event"]["after_execution_id"] is None and row["source"] == "rpc"
    else:
        assert result["status"] == "refused" and not witness["root_started"]
        assert result["root_history"] is None and not root_events(report)
    return result


@pytest.mark.parametrize("case", ["compact", "empty-hint", "whitespace-hint", "hint-boundary",
    "keep-zero", "no-system", "wake", "clear-screen", "duplicate-apply"])
def test_manual_compaction_preserves_exact_request_tail_and_canonical_publication(tmp_path, case):
    report = launch(tmp_path, case)
    result = assert_settled(report, "compacted", "compacted")
    assert result["witness"]["publication_attempted"] and result["witness"]["publication_verified"]
    assert report["snapshot"]["plan"]["tail_messages"] == len(report["expected_tail"])
    assert report["snapshot"]["plan"]["head_context_sha256"] == digest(report["expected_head"])
    assert report["snapshot"]["plan"]["tail_context_sha256"] == digest(report["expected_tail"])
    root_calls = [row for row in report["sdk_requests"] if row["kind"] == "compact"]
    assert len(root_calls) == 1
    assert root_calls[0]["request"]["messages"] == report["expected_compaction_request"]
    assert root_calls[0]["request"]["max_tokens"] == 256
    assert report["review"]["hint"] == report["hint"]
    assert report["review"]["hint_sha256"] == hashlib.sha256(report["hint"].encode("utf-8")).hexdigest()
    truncated = [r for r in report["records_after"][len(report["records_before"]):] if r.get("type") == "truncate"]
    assert len(truncated) == 1 and truncated[0]["reason"] == "compact"
    assert truncated[0]["prepend"] == [
        {"role": "user", "content": "[Summary of earlier conversation, which has been compacted away]\n\n" + SUMMARY},
        {"role": "assistant", "content": "Understood — I have that context."}]
    expected_root = ([{"role": "system", "content": "COMPACT SYSTEM: preserve this exact instruction and Unicode 雪."}]
        if report["original"][0]["role"] == "system" else []) + truncated[0]["prepend"] + report["expected_tail"]
    assert result["root_history"]["context_sha256"] == digest(expected_root)
    assert len(report.get("clear_screen_calls", [])) == (1 if case == "clear-screen" else 0)
    if case == "wake":
        assert result["execution"]["segments"] == 1 and result["execution"]["last_execution_id"]
        wake = [row for row in report["sdk_requests"] if row["kind"] == "wake"]
        assert len(wake) == 1 and wake[0]["request"]["messages"] == report["expected_wake_request"]
    else:
        assert result["execution"] == {"segments": 0, "last_execution_id": None}
        assert not [row for row in report["wire_events"] if row.get("type") == "turn_start"]
    if case == "duplicate-apply":
        assert report["duplicate_response"]["ok"] is False


@pytest.mark.parametrize(("case", "outcome", "code"), [
    ("too-short", "no_change", "too_short"), ("already-recent", "no_change", "already_recent"),
    ("empty-summary", "failed", "no_summary"), ("readiness-failure", "failed", "provider_failed"),
    ("provider-failure", "failed", "provider_failed")])
def test_no_change_and_clean_failure_have_typed_outcomes_without_publication_or_wake(tmp_path, case, outcome, code):
    report = launch(tmp_path, case)
    result = assert_settled(report, outcome, code)
    assert report["live"] == report["original"]
    assert not result["witness"]["publication_attempted"] and not result["witness"]["publication_verified"]
    assert result["execution"] == {"segments": 0, "last_execution_id": None}
    assert not [r for r in report.get("sdk_requests", []) if r["kind"] == "wake"]
    if case in {"too-short", "already-recent", "readiness-failure"}:
        assert report.get("sdk_requests", []) == []


@pytest.mark.parametrize("case", ["tools-on", "tools-off", "tool-then-empty", "tool-cap"])
def test_canonical_tool_policy_and_durable_effects_survive_summary_outcomes(tmp_path, case):
    report = launch(tmp_path, case)
    failed = case in {"tool-then-empty", "tool-cap"}
    result = assert_settled(report, "failed" if failed else "compacted", "no_summary" if failed else "compacted")
    assert report["artifact_exists"] is (case != "tools-off")
    if case != "tools-off":
        assert report["artifact_text"] == "A real compaction tool wrote this disposable artifact.\n"
    calls = [r["request"] for r in report["sdk_requests"] if r["kind"] == "compact"]
    assert all(call["tools"] for call in calls), "Canonical compaction advertises tools even when execution is off"
    assert calls[0]["messages"] == report["expected_compaction_request"]
    assert len(calls) == 2
    assert calls[1]["messages"][:-2] == calls[0]["messages"]
    assert calls[1]["messages"][-2]["role"] == "assistant" and calls[1]["messages"][-1]["role"] == "tool"
    if case == "tools-off":
        assert "disabled" in calls[1]["messages"][-1]["content"].lower() or "off" in calls[1]["messages"][-1]["content"].lower()
    if failed:
        assert not result["witness"]["publication_attempted"]
        assert report["live"] == report["original"]


@pytest.mark.parametrize("case", ["foreign-session", "stale-selection", "readonly", "pending-image", "held",
    "native-owned", "native-app-server", "owner-mode", "unsupported-config", "hint-over-limit", "hint-nul"])
def test_preflight_refusals_never_execute_or_change_history(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["refused_at"] in {"snapshot", "eligibility", "validate"}
    assert report["live"] == report["durable"] == report["original"]
    assert report.get("sdk_requests", []) == [] and not root_events(report)


@pytest.mark.parametrize("case", ["stale-settings", "stale-history", "stale-policy", "wrong-root", "bool-version"])
def test_apply_refusals_do_not_reinterpret_a_changed_review(tmp_path, case):
    report = launch(tmp_path, case)
    assert report.get("sdk_requests", []) == [] and not root_events(report)
    if case in {"wrong-root", "bool-version"}:
        assert report["apply_response"]["ok"] is False
    else:
        result = assert_settled(report, "no_change", "admission_refused")
        assert report["apply"]["status"] == "refused"
        assert result["history"] == report["apply"]["refusal_history"]
    assert report["after"]["disk"] == report["before_apply"]["disk"]


@pytest.mark.parametrize("case", ["persist-silent", "persist-torn", "persist-wrong", "persist-receipts", "persist-system-edit"])
def test_uncertain_publication_never_becomes_success_or_wakes(tmp_path, case):
    report = launch(tmp_path, case)
    assert report["apply"]["status"] == "accepted"
    assert report["settled_response"]["ok"] is False
    assert report["publication_attempts"] >= 1
    assert not [r for r in report["sdk_requests"] if r["kind"] == "wake"]
    assert not [r for r in report["wire_events"] if r.get("type") == "compaction" and r.get("reason") == "compacted"]


@pytest.mark.parametrize("case", ["question", "question-wake", "approval", "approval-wake", "approval-denied"])
def test_real_pending_input_is_answered_only_through_correlated_rpc(tmp_path, case):
    report = launch(tmp_path, case)
    result = assert_settled(report, "cancelled" if case == "approval-denied" else "compacted",
        "cancelled" if case == "approval-denied" else "compacted")
    assert_input_scope(report)
    requested = report["input_request"]
    assert requested["operation_id"] == report["apply"]["operation_id"]
    assert requested["execution_id"]
    if "question" in case:
        assert report["answer_response"]["result"]["answered"] == requested["event"]["id"]
        assert report["question_threads"] and all("SUBMITTED" in t["result"] for t in report["question_threads"])
        assert result["execution"]["segments"] == (1 if "wake" in case else 0)
    else:
        assert report["approval_response"]["result"]["approved"] == requested["event"]["id"]
        assert report["artifact_exists"] is (case in {"approval", "approval-wake"})


@pytest.mark.parametrize("case", ["stop-stream", "stop-readiness", "stop-close", "stop-question",
    "stop-question-wake", "stop-approval", "stop-approval-wake"])
def test_stop_joins_exact_pending_work_and_prevents_later_wake(tmp_path, case):
    report = launch(tmp_path, case)
    result = assert_settled(report, "compacted" if case.endswith("-wake") else "cancelled",
        "compacted" if case.endswith("-wake") else "cancelled")
    assert report["stop_response"]["result"]["stopped"]
    if "input_request" in report:
        assert_input_scope(report, cancelled=True)
    if not case.endswith("-wake"):
        assert not result["witness"]["publication_attempted"] and not result["execution"]["segments"]
    assert not report["artifact_exists"]
    assert all("ABORTED" in t["result"] for t in report["question_threads"])


@pytest.mark.parametrize("case", ["quit-stream", "eof-stream", "quit-readiness", "eof-readiness",
    "quit-question", "eof-question", "quit-question-wake", "eof-question-wake", "quit-approval", "eof-approval", "quit-approval-wake", "eof-approval-wake"])
def test_quit_and_real_pipe_eof_retire_workers_inputs_and_callbacks_before_lease_release(tmp_path, case):
    report = launch(tmp_path, case)
    if "input_request" in report:
        assert_input_scope(report, cancelled=True)
    proof = report["shutdown"]
    assert proof["worker_settled"] and proof["admission_settled"] and proof["hooks_settled"]
    assert proof["effects_verified"] is False and proof["pending_at_close"]
    assert proof["action"] == "compact" and proof["action_id"] == report["review"]["action_id"]
    assert proof["operation_id"] == report["apply"]["operation_id"]
    assert proof["current_session_id"] == proof["session_id"] == report["before"]["identity"]["session_id"]
    released = [row for row in report["events"] if row["effect"] == "compact-lease-release"]
    assert released and all(not row["active_chat"] and row["threads_retired"] for row in released)
    assert not report["artifact_exists"]
    assert all("ABORTED" in t["result"] for t in report["question_threads"])


@pytest.mark.parametrize("case", ["legacy-gui-compact", "legacy-prompt", "legacy-prompt-case",
    "legacy-run-command", "legacy-run-command-space", "legacy-gui-prompt"])
def test_negotiated_legacy_routes_refuse_before_any_admission_or_effect(tmp_path, case):
    report = launch(tmp_path, case)
    response = report["legacy_response"]
    assert response["ok"] is False
    assert "reviewed native" in response["error"].lower() or "structured management" in response["error"].lower()
    assert report["legacy_before"] == report["legacy_after"]
    assert report["live"] == report["durable"] == report["original"]
    assert report.get("sdk_requests", []) == [] and not root_events(report)
    assert not [c for c in report["commands"] if c["type"].startswith("gui.conversations.native_compact_")]


def test_permission_drift_after_provider_request_refuses_before_first_publication_write(tmp_path):
    report = launch(tmp_path, "late-readonly")
    assert report["late_readonly_injected"]
    assert report["apply"]["status"] == "accepted"
    assert report["settled_response"]["ok"] is False
    assert report.get("owned_write_attempts", []) == []
    assert report["live"] == report["durable"] == report["original"]
    assert report["after"]["disk"] == report["before_apply"]["disk"]
    assert report["shutdown"]["mutated"] is False and report["shutdown"]["effects_verified"] is False
    assert not [r for r in report["sdk_requests"] if r["kind"] == "wake"]
