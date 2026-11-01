"""Independent real-source preference edits and subsequent canonical compaction."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/compaction_settings_runtime.py"
FIELDS = {"compact_keep_recent": 2, "compact_max_tokens": 768, "compact_max_tool_iters": 3,
          "compact_thinking_level": "high", "wake_after_compact": True, "clear_screen_after_compact": False}
PREFIX = "gui.settings.native_compact_"


def command(tmp_path, case="success", field="compact_max_tokens", value=768, driver="self"):
    root = tmp_path / "data"
    root.mkdir()
    settings = {"backend": "custom", "default_model": "switch-old", "backend_chosen": True,
                "thinking_level": "default", "user_name_asked": True,
                "tools_enabled": case.startswith("consume") and case != "consume-tools-off" or case == "round-cap",
                "mcp_enabled": False, "skills_enabled": False, "footer_task_manager": False,
                "tts_enabled": False, "sidecar_enabled": False, "tool_policy_profile": "strict",
                "autocompact_enabled": False, "tool_auto_background_s": 0,
                "custom_base_url": "http://127.0.0.1:45120/v1", "custom_context_length": 1000000,
                "compact_keep_recent": 4, "compact_max_tokens": 12288, "compact_max_tool_iters": 8,
                "compact_thinking_level": "low", "wake_after_compact": False, "clear_screen_after_compact": True}
    (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_COMPACT_SETTINGS_SOURCE=str(source), LITETUI_DATA_ROOT=str(root),
        RUST_COMPACT_SETTINGS_CASE=case, RUST_COMPACT_SETTINGS_FIELD=field,
        RUST_COMPACT_SETTINGS_VALUE=json.dumps(value), RUST_COMPACT_SETTINGS_DRIVER=driver,
        PYTHONPATH=str(ROOT / "tests"), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    for key in ("LITETUI_MAX_TOKENS", "LITETUI_TEST_COMPACT_CAP"):
        environment.pop(key, None)
    args = [os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable), "-B", str(FIXTURE), "--rpc",
        "--hold-restored-input", "--history-projection-version", "1", "--backend", "custom", "--model", "switch-old",
        "--thinking-level", "default", "--tool-profile", "strict", "--mcp-servers", "none",
        "--server-mode", "connect", "--server-timeout", "5", "--base-url", "http://127.0.0.1:45120/v1"]
    return args, environment, root, source


def read_report(root, source):
    report = json.loads((root / "compaction-settings-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == source / "src/litetui/app.py"
    assert report["denied"] == []
    assert all(worker["finished"] for worker in report["workers"]), report["workers"]
    assert all(stream["closed"] for stream in report["streams"]), report["streams"]
    assert not report["question_pending"] and not report["approval_pending"] and not report["approval_authorities"]
    assert report["lease_release"] and all(not row["active_chat"] and not row["questions"] and not row["approvals"]
                                            for row in report["lease_release"])
    assert report["archive_prefix_preserved"]
    assert report["durable"] == report["after"]["history"]
    return report


def launch(tmp_path, *, case="success", field="compact_max_tokens", value=768):
    args, environment, root, source = command(tmp_path, case, field, value)
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=110, check=False)
    (tmp_path / "events.jsonl").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    return read_report(root, source)


def expect_edit_only(report):
    assert report["sdk_requests"] == [] and report["streams"] == [] and report["clear_screen_calls"] == []
    assert report["before"]["history"] == report["after"]["history"]
    for key in ("identity", "authority", "tools", "pending", "handles"):
        assert report["after"][key] == report["before"][key], key


@pytest.mark.parametrize("field,value", FIELDS.items())
def test_real_six_field_saves_use_exact_destination_without_execution(tmp_path, field, value):
    report = launch(tmp_path, field=field, value=value)
    expect_edit_only(report)
    receipt = report["result"]["result"]
    assert receipt["status"] == "saved" and receipt["saved"] == receipt["effective"] == value
    assert receipt["mutated"] and not receipt["close_required"]
    destination = "global" if field == "clear_screen_after_compact" else "conversation"
    expected = json.loads(report["initial_documents"][destination]["raw"])
    (expected if destination == "global" else expected["execution"])[field] = value
    assert json.loads(report["documents"][destination]["raw"]) == expected
    assert receipt["destination"] == report["documents"][destination]["path"]
    assert receipt["persistence"][0]["destination"] == receipt["destination"]
    for key in ("global", "conversation", "agent"):
        if key != destination:
            assert report["documents"][key] == report["initial_documents"][key]
    old = dict(report["before"]["settings"])
    old[field] = value
    assert report["after"]["settings"] == old
    assert report["readback"]["fields"][field]["saved"] == report["readback"]["fields"][field]["effective"] == value


@pytest.mark.parametrize("case", ["cancel", "invalid", "stale", "superseded", "duplicate"])
def test_cancel_stale_invalid_and_single_use_review_never_infer(tmp_path, case):
    report = launch(tmp_path, case=case)
    expect_edit_only(report)
    if case in {"cancel", "invalid"}:
        assert not report["saves"]
        assert report["initial_disk"] == report["final_disk"]
        assert report["before"] == report["after"]
    elif case == "duplicate":
        assert report["saves"][0]["status"] == "saved"
        assert report["duplicate"]["result"]["status"] == "refused"
        assert report["after_save_disk"] == report["final_disk"]
    else:
        assert report["result"]["result"]["status"] == "refused"
        assert report["before"] == report["after"]
        assert report["final_disk"] == report.get("stale_disk", report["initial_disk"])


@pytest.mark.parametrize("field,value", [("compact_max_tokens", 768), ("clear_screen_after_compact", False)])
@pytest.mark.parametrize("case", ["persistence-refused", "silent", "torn", "wrong-field", "drop-unknown",
                                 "write-then-error", "runtime-failed", "runtime-raised"])
def test_real_both_scope_persistence_and_runtime_faults_have_honest_receipts(tmp_path, field, value, case):
    report = launch(tmp_path, case=case, field=field, value=value)
    expect_edit_only(report)
    receipt = report["result"]["result"]
    expected = "refused" if case == "persistence-refused" else "partial" if case == "runtime-failed" else "failed"
    assert receipt["status"] == expected, receipt
    assert receipt["mutated"] is (case != "persistence-refused")
    assert receipt["close_required"] is (expected == "failed")
    assert report["after"]["settings"][field] == report["before"]["settings"][field]
    if case == "persistence-refused":
        assert report["initial_disk"] == report["final_disk"]
    if case == "runtime-failed":
        assert receipt["saved"] == value
        assert receipt["effective"] == report["before"]["settings"][field]


@pytest.mark.parametrize("field,value", [("compact_max_tokens", 768), ("clear_screen_after_compact", False)])
def test_actual_missing_conversation_settings_materializes_only_conversation_save(tmp_path, field, value):
    report = launch(tmp_path, case="absent", field=field, value=value)
    expect_edit_only(report)
    assert report["initial_documents"]["conversation"]["raw"] is None
    assert report["result"]["result"]["status"] == "saved"
    assert (report["documents"]["conversation"]["raw"] is not None) is (field != "clear_screen_after_compact")


@pytest.mark.parametrize("case", ["consume", "consume-no-wake", "consume-no-clear", "consume-tools-off", "round-cap"])
def test_next_actual_compaction_uses_all_saved_preferences_and_real_round_bound(tmp_path, case):
    report = launch(tmp_path, case=case)
    saved = report["saved_preferences"]
    assert len(report["saves"]) == 6 and all(row["status"] == "saved" for row in report["saves"])
    assert report["compact_snapshot"]["config"] == {
        "keep_recent": 2, "max_tokens": 768, "max_tool_iters": saved["compact_max_tool_iters"],
        "thinking_level": "high", "wake_after_compact": saved["wake_after_compact"],
        "clear_screen_after_compact": saved["clear_screen_after_compact"],
        "tools_enabled": report["before"]["tools"]}
    requests = [row["request"] for row in report["sdk_requests"] if row["kind"] == "compact"]
    assert len(requests) == (2 if case == "round-cap" else 1 if case == "consume-tools-off" else 3)
    assert all(row["max_tokens"] == 768 and row["extra_body"]["reasoning_effort"] == "high" for row in requests)
    original = report["before"]["history"]
    expected_first = [*original[:-2], {"role": "user",
        "content": report["compact_review"]["hint"]}]
    assert requests[0]["messages"][:-1] == expected_first[:-1]
    assert requests[0]["messages"][-1]["content"].endswith("\n\nKeep the tested settings.")
    assert report["compact_snapshot"]["plan"]["tail_messages"] == 2
    settled = report["compact_settled"]["result"]
    if case == "round-cap":
        assert settled["outcome"] == "failed" and settled["reason_code"] == "no_summary"
        assert report["after"]["history"] == original
    else:
        assert settled["outcome"] == "compacted", settled
        assert report["after"]["history"][3:5] == original[-2:]
    wakes = [row for row in report["sdk_requests"] if row["kind"] == "wake"]
    assert bool(wakes) is (saved["wake_after_compact"] and case != "round-cap")
    assert len(report["clear_screen_calls"]) == int(saved["clear_screen_after_compact"] and case != "round-cap")
    if case != "consume-tools-off":
        # Real unknown-tool refusal is fed back; the fixture never replaces dispatch.
        assert all(any(row.get("role") == "tool" for row in request["messages"]) for request in requests[1:])


@pytest.mark.parametrize("ending", ["quit", "eof", "delayed-quit", "delayed-eof"])
@pytest.mark.parametrize("field,value", [("compact_max_tokens", 768), ("clear_screen_after_compact", False)])
def test_synchronous_save_receipt_precedes_real_quit_or_eof_cleanup(tmp_path, ending, field, value):
    args, environment, root, source = command(tmp_path, case="delay-save" if ending.startswith("delayed-") else "success",
                                                 field=field, value=value, driver="rpc")
    lines: queue.Queue = queue.Queue()
    captured: list[str] = []
    errors: list[str] = []
    process = subprocess.Popen(args, cwd=tmp_path, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None

    def read_stdout():
        assert process.stdout is not None
        for line in process.stdout:
            captured.append(line)
            lines.put(json.loads(line))

    def read_stderr():
        assert process.stderr is not None
        errors.append(process.stderr.read())

    readers = [threading.Thread(target=read_stdout), threading.Thread(target=read_stderr)]
    for reader in readers:
        reader.start()

    def send(operation, identity, **fields):
        command = {"type": operation, "id": identity, **fields}
        assert process.stdin is not None
        process.stdin.write(json.dumps(command) + "\n")
        process.stdin.flush()

    def response(identity):
        while True:
            row = lines.get(timeout=40)
            if row.get("type") == "response" and row.get("id") == identity:
                assert row["ok"], row
                return row["result"]

    try:
        send("gui.hello", "shutdown-hello", protocol_version=1)
        hello = response("shutdown-hello")
        assert PREFIX + "save" in hello["operations"]
        send("gui.state", "shutdown-state", include_conversations=False)
        current = response("shutdown-state")
        # The canonical state exposes runtime identity under the existing fields.
        identity = {"version": 1, "session_id": current["session_id"], "agent_id": current["ownership"]["agent_id"],
                    "backend": current["model"]["backend"], "model": current["model"]["model"]}
        send(PREFIX + "snapshot", "shutdown-snapshot", **identity)
        snapshot = response("shutdown-snapshot")
        send(PREFIX + "validate", "shutdown-review", **identity, snapshot=snapshot["snapshot"],
             revisions=snapshot["revisions"], field=field, value=value)
        review = response("shutdown-review")
        send(PREFIX + "save", "shutdown-save", **{key: val for key, val in review.items() if key != "kind"})
        if ending.startswith("delayed-"):
            marker = root / "compaction-settings-stage.json"
            deadline = time.monotonic() + 30
            while not marker.exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            stage = json.loads(marker.read_text(encoding="utf-8"))
            assert stage["stage"] == "save-response" and stage["operation_id"] == "shutdown-save"
            assert stage["status"] == "saved" and stage["field"] == field and stage["value"] == value
        if ending.endswith("quit"):
            send("shutdown", "shutdown-final")
        process.stdin.close()
        assert process.wait(timeout=45) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        (tmp_path / "events.jsonl").write_text("".join(captured), encoding="utf-8")
        (tmp_path / "stderr.txt").write_text("".join(errors), encoding="utf-8")
    assert all(not reader.is_alive() for reader in readers)
    report = read_report(root, source)
    expect_edit_only(report)
    assert len(report["saves"]) == 1
    if ending.startswith("delayed-"):
        assert report["buffer_release"] == {"cause": ending.removeprefix("delayed-"), "operation_id": "shutdown-save"}
        assert any(row.get("id") == "shutdown-save" and row.get("ok") for row in report["wire_events"])
    receipt = report["saves"][0]
    assert receipt["status"] in {"saved", "refused"}, receipt
    if receipt["status"] == "saved":
        assert receipt["saved"] == receipt["effective"] == value
    else:
        assert report["final_disk"] == report["initial_disk"]
    assert not any(command["type"] in {PREFIX + "snapshot", "gui.state"} for command in
                   report["commands"][next(i for i, cmd in enumerate(report["commands"])
                                           if cmd["type"] == PREFIX + "save") + 1:])
