"""Actual-source saved-row inspection without opening or replacing active context."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/history_inspection_runtime.py"
EXPLICIT = "Explicit original conversation prompt after saved-row inspection"


def launch(tmp_path, case):
    root = tmp_path / "data"
    root.mkdir()
    settings = {"backend": "custom", "default_model": "switch-old", "backend_chosen": True,
                "thinking_level": "default", "user_name_asked": True, "tools_enabled": False,
                "mcp_enabled": False, "skills_enabled": False, "footer_task_manager": False,
                "tts_enabled": False, "sidecar_enabled": False, "tool_policy_profile": "strict",
                "autocompact_enabled": False, "tool_auto_background_s": 0,
                "custom_base_url": "http://127.0.0.1:45120/v1", "custom_context_length": 1000000,
                "max_tokens_chat": 101, "max_tokens_tools": 202, "theme_name": "textual-dark", "custom_themes": {}}
    (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="inspection-" + case,
                       RUST_HISTORY_PHASE="fresh", RUST_HISTORY_DRIVER="self", RUST_INSPECTION_CASE=case,
                       RUST_INSPECTION_DRIVER="self", LITETUI_DATA_ROOT=str(root), PYTHONPATH=str(ROOT / "tests"),
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    result = subprocess.run([python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input",
                             "--history-projection-version", "1", "--backend", "custom", "--model", "switch-old",
                             "--tool-profile", "strict", "--thinking-level", "default", "--mcp-servers", "none",
                             "--server-mode", "connect", "--server-timeout", "5",
                             "--base-url", "http://127.0.0.1:45120/v1"], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    (tmp_path / "events.jsonl").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / "history-fresh-report.json").read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == source / "src/litetui/app.py"
    assert report["denied"] == []
    assert report["provider_calls"] == report["provider_requests"] == report["interactive_events"] == []
    assert report["read_checks"] >= 2
    assert report["maximum_catalog_wire"] <= 256 * 1024
    assert all(row["wire_bytes"] <= (256 * 1024 if row["type"] == "gui.conversations.page" else 1024 * 1024)
               for row in report["observations"])
    assert all(worker["finished"] for worker in report["worker_settlement"])
    forbidden = {"gui.conversations.native_open", "gui.conversations.open", "gui.restored_input.continue",
                 "gui.restored_input.resolve", "gui.restored_input.cancel", "gui.conversations.read"}
    assert not any(command["type"] in forbidden for command in report["commands"])
    return report


def assert_original_prompt(report):
    assert len(report["requests"]) == 1
    request = report["requests"][0]
    assert request["complete_prepared_context"]
    assert request["context_sha256"] == request["prepared_sha256"]
    assert request["wire_row_hashes"] == request["expected_row_hashes"]
    assert request["wire_roles"] == [row["role"] for row in report["active_context"]] + ["user"]
    assert request["users"] == [row["content"] for row in report["active_context"] if row["role"] == "user"] + [EXPLICIT]
    assert request["session_id"] == report["before"]["session_id"] == report["after"]["session_id"]
    assert request["agent_id"] == report["before"]["agent_id"]
    assert request["backend"] == "custom" and request["model"] == "switch-old"
    assert report["before_prompt"] == report["before"]
    assert sum(command["type"] == "prompt" for command in report["commands"]) == 1


@pytest.mark.parametrize("case", ["current", "saved", "readonly", "incompatible", "pending"])
def test_current_and_saved_rows_are_inert_then_only_original_context_reaches_explicit_sdk_request(tmp_path, case):
    report = launch(tmp_path, case)
    inspection = report["inspection"]
    descriptor = inspection["descriptor"]
    assert descriptor["from_session_id"] == report["before"]["session_id"]
    assert descriptor["backend"] == "custom" and descriptor["model"] == "switch-old"
    assert descriptor["scope"] == ("current" if case == "current" else "preview")
    if case == "current":
        assert inspection["catalog"] is None
        assert descriptor["session_id"] == report["before"]["session_id"]
    else:
        target = report["targets"][0]
        assert descriptor["session_id"] == target["session_id"] != report["before"]["session_id"]
        assert descriptor["context_sha256"] == target["context_sha256"]
        row = next(row for row in inspection["catalog"]["entries"] if row["session_id"] == target["session_id"])
        assert row["can_open"] is (case not in {"readonly", "incompatible"})
        if case == "incompatible":
            assert "tools disabled" in row["reason"]
    assert descriptor["read_only"] is (case == "readonly")
    if case == "pending":
        assert report["before"]["pending"] and report["before_prompt"]["pending"]
        assert not report["after"]["pending"]
    assert_original_prompt(report)


@pytest.mark.parametrize("case", ["large", "rich", "unicode"])
def test_large_rich_and_unicode_saved_targets_page_completely_without_becoming_active(tmp_path, case):
    report = launch(tmp_path, case)
    inspection = report["inspection"]
    blocks = inspection["walk"]["blocks"]
    assert len(blocks) == inspection["descriptor"]["counts"]["blocks"]
    assert inspection["descriptor"]["context_sha256"] == report["targets"][0]["context_sha256"]
    if case == "large":
        assert inspection["descriptor"]["counts"]["messages"] == 1203
        assert inspection["walk"]["pages"] > 1 and len(blocks) == 1202
    elif case == "unicode":
        expected = ("🛰️雪e\u0301\n" * 5000).encode("utf-8")
        assert blocks[-1]["bytes"] == len(expected)
        assert blocks[-1]["block_sha256"] == blocks[-1]["text_sha256"] == hashlib.sha256(expected).hexdigest()
        assert inspection["walk"]["pages"] > 2
    else:
        displayed = json.dumps(blocks)
        assert {"tool", "plan", "reasoning", "notice", "attachment", "summary"} <= {row["kind"] for row in blocks}
        assert "Latest saved question" in displayed
        assert "Stale saved question" not in displayed and "Terminal saved question" not in displayed
        assert "NOT_REAL_IMAGE" not in displayed and "NEVER_EMIT_UNKNOWN_BASE64" not in displayed
        assert "Hidden duplicate Codex fallback" not in displayed
    assert_original_prompt(report)


def test_inspection_preserves_real_held_ledger_handles_and_never_releases_input(tmp_path):
    report = launch(tmp_path, "held")
    assert len(report["before"]["queue"]) == 1
    assert report["before"] == report["after"]
    assert report["requests"] == []


def test_locator_uses_ordinary_aligned_pages_at_boundaries_and_last_row(tmp_path):
    report = launch(tmp_path, "locate-boundaries")
    assert [(row["index"], row["offset"]) for row in report["locations"]] == [(49, 0), (50, 50), (99, 50), (100, 100), (105, 100)]
    assert report["before"] == report["after"] and report["requests"] == []


def test_locator_reordering_finds_same_uuid_with_fresh_token_without_switching(tmp_path):
    report = launch(tmp_path, "reorder")
    old, new = report["inspection"], report["refreshed"]
    assert old["catalog"]["offset"] == 100 and new["catalog"]["offset"] == 0
    assert old["descriptor"]["session_id"] == new["descriptor"]["session_id"] == report["targets"][-1]["session_id"]
    assert old["catalog"]["snapshot"] != new["catalog"]["snapshot"]
    assert report["before"] == report["after"] and report["requests"] == []
    assert len(report["refusals"]) == 1


@pytest.mark.parametrize("case", ["delete", "delete-after-locate", "edit-after-locate", "edit-after-snapshot", "policy-after-locate"])
def test_stale_target_or_policy_refuses_old_inspection_without_following_a_neighbor(tmp_path, case):
    report = launch(tmp_path, case)
    assert len(report["refusals"]) == (2 if case.startswith("delete") else 1)
    assert report["before"] == report["after"] and report["requests"] == []
    if not case.startswith("delete"):
        assert report["inspection"]["descriptor"]["session_id"] == report["refreshed"]["descriptor"]["session_id"]


def test_locator_refuses_null_malformed_noncanonical_foreign_and_cursor_combinations(tmp_path):
    report = launch(tmp_path, "invalid")
    assert len(report["refusals"]) == 8
    assert report["before"] == report["after"] and report["requests"] == []
