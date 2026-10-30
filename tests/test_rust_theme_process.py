"""Controlled owned CLI theme persistence, registration and restart contracts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "rust/litetui-terminal/tests/fixtures/theme_runtime.py"
FIELDS = {"background", "surface", "primary", "panel", "tool_background", "foreground",
          "title", "assistant_border", "thinking_text", "thinking_border", "tool_text",
          "error", "footer_foreground", "footer_background"}


def launch(tmp_path, *, case="select", phase="fresh", resume=None, name="  Theme 雪  "):
    root = tmp_path / "data"
    if phase == "fresh":
        root.mkdir()
        settings = {"backend": "custom", "default_model": "switch-old", "backend_chosen": True,
                    "thinking_level": "default", "user_name_asked": True,
                    "tools_enabled": False, "mcp_enabled": False, "skills_enabled": False,
                    "footer_task_manager": False, "tts_enabled": False, "sidecar_enabled": False,
                    "tool_policy_profile": "strict", "autocompact_enabled": False,
                    "tool_auto_background_s": 0, "custom_base_url": "http://127.0.0.1:45120/v1",
                    "custom_context_length": 8192, "max_tokens_chat": 101, "max_tokens_tools": 202,
                    "theme_name": "textual-dark", "custom_themes": {}}
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    runtime_source = Path(os.environ.get("LITETUI_TEST_RUNTIME_SOURCE", str(ROOT))).resolve()
    environment = dict(os.environ, RUST_THEME_SOURCE=str(runtime_source), RUST_THEME_DRIVER="self",
                       RUST_THEME_CASE=case, RUST_THEME_PHASE=phase, RUST_THEME_NAME=name,
                       LITETUI_DATA_ROOT=str(root), PYTHONPATH=str(ROOT / "tests"),
                       PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    environment.pop("LITETUI_MAX_TOKENS", None)
    environment.pop("RUST_THEME_TOKENS", None)
    python = os.environ.get("LITETUI_TEST_RUNTIME_PYTHON", sys.executable)
    args = [python, "-B", str(FIXTURE), "--rpc", "--hold-restored-input",
            "--backend", "custom", "--model", "switch-old", "--thinking-level", "default",
            "--tool-profile", "strict", "--mcp-servers", "none", "--server-mode", "connect",
            "--base-url", "http://127.0.0.1:45120/v1", "--server-timeout", "5"]
    if resume:
        args.extend(["--convo", resume])
    result = subprocess.run(args, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=75, check=False)
    (tmp_path / (phase + "-events.jsonl")).write_text(result.stdout, encoding="utf-8")
    (tmp_path / (phase + "-stderr.txt")).write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / ("theme-" + phase + "-report.json")).read_text(encoding="utf-8"))
    assert Path(report["source_app"]).resolve() == runtime_source / "src/litetui/app.py"
    assert report["denied"] == []
    return report


def selected(snapshot):
    row, = [row for row in snapshot["rows"] if row["row_id"] == snapshot["selection"]["effective_row_id"]]
    assert row["name"] == snapshot["selection"]["effective_name"]
    assert row["definition"] == snapshot["selection"]["effective_definition"]
    assert set(row["palette"]) == FIELDS
    assert all(len(rgb) == 3 and all(type(value) is int and 0 <= value <= 255 for value in rgb)
               for rgb in row["palette"].values())
    return row


def assert_device_only_save(report):
    assert report["snapshot_unchanged"] and report["validate_unchanged"]
    result = report["result"]
    assert result["status"] == "saved" and result["mutated"] and not result["close_required"]
    assert result["runtime"]["status"] == "applied"
    assert report["after_save_identity"] == report["before"]
    global_path = next(path for path in report["initial_disk"] if path.endswith(("data\\settings.json", "data/settings.json")))
    assert all(before == report["after_save_disk"][path]
               for path, before in report["initial_disk"].items() if path != global_path)
    writes = report["writes_at_save"]
    assert len(writes) == 1 and writes[0]["kind"] == "service" and writes[0]["in_save"]
    assert Path(writes[0]["path"]) == Path(global_path)
    persistence, = result["persistence"]
    assert persistence["saved"] and persistence["scope"] == "device"
    assert Path(persistence["destination"]) == Path(global_path)
    assert set(persistence["fields"]) == set(report["review"]["fields"])
    row = selected(report["readback"])
    assert row["palette"] == report["after_save"]["palette"]
    assert row["definition"] == report["review"]["target"]["definition"]
    assert row["name"] == report["after_save"]["runtime_name"] == report["after_save"]["effective_name"]
    assert len(report["requests"]) == 1
    assert report["requests"][0]["profile"] == "strict" and report["requests"][0]["tools_enabled"] is False


@pytest.mark.parametrize("case,target", [("select", "matrix"), ("custom", "Theme 雪")])
def test_saved_selection_and_created_palette_survive_restart_without_provider_mutation(tmp_path, case, target):
    report = launch(tmp_path, case=case)
    assert_device_only_save(report)
    assert report["saved_document"]["theme_name"] == target
    assert report["after_save"]["effective_name"] == target
    resumed = launch(tmp_path, case=case, phase="resume", resume=report["after"]["session_id"])
    assert resumed["before"]["session_id"] == report["after"]["session_id"]
    assert resumed["before"]["agent_id"] == report["after"]["agent_id"]
    assert resumed["theme_before"]["palette"] == report["after_save"]["palette"]
    assert selected(resumed["snapshot"])["name"] == target
    assert resumed["snapshot"]["selection"]["saved_name"] == target
    assert len(resumed["requests"]) == 1
    assert not resumed["writes"]


def test_overwriting_active_name_registers_new_colors_and_refreshes_without_second_save(tmp_path):
    report = launch(tmp_path, case="overwrite")
    assert_device_only_save(report)
    assert report["review"]["overwrite"]["custom"] and report["review"]["overwrite"]["registered"]
    assert report["theme_before"]["runtime_name"] == report["after_save"]["runtime_name"] == "Theme 雪"
    assert report["theme_before"]["palette"] != report["after_save"]["palette"]
    assert report["css_after_save"] > report["css_before"]
    assert any(row["effect"] == "refresh" and row["in_save"] for row in report["events_at_save"])
    assert any(row["effect"] == "footer" for row in report["events_at_save"])
    assert report["after_save"]["palette"]["primary"] == [18, 52, 86]


@pytest.mark.parametrize("name", ["matrix", "textual-dark", "textual-light"])
def test_catalog_uses_exact_registered_custom_shadow_including_dark_light_name(tmp_path, name):
    report = launch(tmp_path, case="shadow-" + name)
    assert report["snapshot_unchanged"] and not report["writes"] and not report["requests"]
    snapshot = report["snapshot"]
    rows = [row for row in snapshot["rows"] if row["name"] == name]
    assert len(rows) == 1 and rows[0]["source"] == "custom"
    assert rows[0]["selectable"]
    assert selected(snapshot)["palette"] == report["theme_before"]["palette"]
    assert selected(snapshot)["palette"]["primary"] == [18, 52, 86]
    assert selected(snapshot)["name"] == name


@pytest.mark.parametrize("case", ["startup-custom", "startup-unknown", "startup-invalid", "startup-case"])
def test_startup_reports_saved_requested_and_actual_resolved_theme_without_mutation(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["snapshot_unchanged"] and not report["requests"] and not report["writes"]
    selection = report["snapshot"]["selection"]
    assert selection["saved_name"] == report["saved_document"]["theme_name"]
    assert selection["requested_name"] == report["theme_before"]["requested_name"]
    assert selection["runtime_name"] == report["theme_before"]["runtime_name"]
    assert selected(report["snapshot"])["palette"] == report["theme_before"]["palette"]
    if case in {"startup-unknown", "startup-invalid"}:
        assert selection["effective_name"] == "textual-dark"
    if case == "startup-case":
        rows = [row for row in report["snapshot"]["rows"] if row["name"] in {"Case Theme", "case theme"}]
        assert len(rows) == 2
        assert selected(report["snapshot"])["name"] == "case theme"
        assert selected(report["snapshot"])["palette"]["primary"] == [171, 205, 239]


@pytest.mark.parametrize("case", ["optional-missing", "optional-invalid"])
def test_optional_tokens_use_canonical_derived_or_omitted_values_in_review_and_runtime(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert_device_only_save(report)
    tokens = {row["key"]: row for row in report["review"]["target"]["tokens"]}
    assert len(tokens) == 15
    for key in ("thinking-text", "thinking-box", "tool-text"):
        assert tokens[key]["source"] == "derived"
    for key in ("footer-foreground", "footer-background"):
        assert tokens[key]["source"] == "omitted"
    palette = report["after_save"]["palette"]
    assert palette["thinking_text"] == [214, 215, 216]
    assert palette["thinking_border"] == [18, 52, 86]
    assert palette["tool_text"] == [232, 163, 61]


@pytest.mark.parametrize("case", ["validate-only", "cancel-validate", "invalid-core", "invalid-name",
                                 "unknown-token", "foreign"])
def test_review_and_invalid_inputs_never_write_register_select_or_prompt(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["snapshot_unchanged"]
    assert not report["writes"] and not report["requests"]
    assert report["initial_disk"] == report["final_disk"]
    assert report["theme_before"] == report["theme_after"]
    if case in {"validate-only", "cancel-validate"}:
        assert report["validate_unchanged"] and "review" in report and "result" not in report
    else:
        assert report["validation_error"] and "result" not in report


def test_registered_comma_name_is_visible_but_not_offered_as_exact_selection(tmp_path):
    report = launch(tmp_path, case="unselectable-name")
    row, = [row for row in report["snapshot"]["rows"] if row["name"] == "comma,name"]
    assert not row["selectable"] and row["reason"]
    assert report["theme_before"] == report["theme_after"]
    assert not report["writes"] and not report["requests"]


@pytest.mark.parametrize("case", ["stale", "registry-drift"])
def test_save_rechecks_global_bytes_and_registered_definition_before_effects(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert report["result"]["status"] == "refused"
    assert not report["result"]["mutated"] and not report["result"]["close_required"]
    assert report["after"]["session_id"] == report["before"]["session_id"]
    assert report["theme_after"]["runtime_name"] == report["theme_before"]["runtime_name"]
    assert not report["writes"] and not report["requests"]
    if case == "stale":
        assert report["saved_document"]["fixture_foreign_device_edit"] == 1


def test_single_use_review_never_repeats_device_write(tmp_path):
    report = launch(tmp_path, case="replay")
    assert_device_only_save(report)
    assert report.get("replay_error") or report["replay"]["status"] == "refused"
    assert len(report["writes"]) == 1


def test_custom_creation_merges_exact_reviewed_disk_map_and_keeps_unrelated_invalid_entries(tmp_path):
    report = launch(tmp_path, case="disk-only")
    assert_device_only_save(report)
    custom = report["saved_document"]["custom_themes"]
    assert custom["other-instance"]["primary"] == "#abcdef"
    assert custom["invalid-preserved"] == {"primary": "broken"}
    assert custom["Theme 雪"]["primary"] == "#123456"


@pytest.mark.parametrize("case", ["write-before", "write-after", "write-corrupt", "register-failed", "select-failed", "refresh-failed"])
def test_persistence_or_runtime_faults_never_claim_effective_success_or_auto_retry(tmp_path, case):
    report = launch(tmp_path, case=case)
    result = report["result"]
    assert result["status"] in {"refused", "partial", "failed"} and result["status"] != "saved"
    assert len(report["requests"]) == int(result["status"] == "partial" and not result["close_required"])
    assert len([row for row in report["commands"] if row["type"] == "gui.themes.native_save"]) == 1
    assert len(report["writes_at_save"]) == 1
    if case == "write-before":
        assert report["initial_disk"] == report["after_save_disk"]
        assert report["theme_before"] == report["after_save"]
    elif case == "write-after":
        assert result["mutated"] and result["status"] == "partial"
        assert not result["persistence"][0]["saved"] and result["runtime"]["status"] == "unchanged"
        assert report["saved_document"]["theme_name"] == "Theme 雪"
        assert report["after_save"]["effective_name"] == report["theme_before"]["effective_name"]
    elif case == "write-corrupt":
        assert result["mutated"] and result["status"] == "failed"
        assert report["saved_document"]["theme_name"] == "uncertain-theme"
        assert result["runtime"]["status"] == "unknown"
    else:
        assert report["saved_document"]["theme_name"] == "Theme 雪"
        assert result["status"] == "partial" or result["close_required"]
        if result["status"] == "partial":
            assert report["readback"]["selection"]["saved_name"] == "Theme 雪"
            assert selected(report["readback"])["palette"] == report["after_save"]["palette"]


def test_oversized_registered_catalog_refuses_without_theme_or_disk_change(tmp_path):
    report = launch(tmp_path, case="oversized-catalog")
    assert report["snapshot_error"]
    assert report["initial_disk"] == report["final_disk"]
    assert report["theme_before"] == report["theme_after"]
    assert not report["writes"] and not report["requests"]



def test_device_theme_save_does_not_materialize_pending_conversation_until_explicit_prompt(tmp_path):
    report = launch(tmp_path, case="empty")
    assert_device_only_save(report)
    assert report["before"]["pending"] and report["after_save_identity"]["pending"]
    assert not any(row.get("role") != "system" for row in report["before"]["history"])
    assert not report["after"]["pending"]


def test_registered_nonhex_colors_match_actual_source_and_token_disclosures(tmp_path):
    report = launch(tmp_path, case="startup-registered")
    row = selected(report["snapshot"])
    assert row["source"] == "registered" and row["name"] == "registered-color"
    assert row["palette"] == report["theme_before"]["palette"]
    assert row["palette"]["tool_text"] == [255, 0, 0]
    assert row["palette"]["thinking_text"] == [0, 0, 255]
    tokens = {item["key"]: item for item in row["tokens"]}
    assert tokens["tool-text"]["source"] == "explicit"
    assert tokens["tool-text"]["resolved"].lower() == "#ff0000"
    assert tokens["thinking-text"]["resolved"].lower() == "#0000ff"
    assert not report["writes"] and not report["requests"]


def test_later_canonical_theme_watcher_does_not_republish_stale_custom_map(tmp_path):
    report = launch(tmp_path, case="later-watch-save")
    assert_device_only_save(report)
    assert report["later_saved_document"]["theme_name"] == "matrix"
    assert report["later_saved_document"]["custom_themes"]["external-after-save"]["primary"] == "#fedcba"
    assert report["later_saved_document"]["custom_themes"]["Theme 雪"]["primary"] == "#123456"
    assert len(report["writes"]) == 2


def test_trimmed_alias_that_would_override_candidate_refuses_before_device_write(tmp_path):
    report = launch(tmp_path, case="alias-lose")
    assert report["validation_error"] and "result" not in report
    assert report["snapshot_unchanged"]
    assert report["initial_disk"] == report["final_disk"]
    assert report["theme_before"] == report["theme_after"]
    assert not report["writes"] and not report["requests"]


def test_trimmed_alias_winner_is_identical_immediately_and_after_restart(tmp_path):
    report = launch(tmp_path, case="alias-win")
    assert_device_only_save(report)
    assert list(report["saved_document"]["custom_themes"]) == [" Theme 雪 ", "Theme 雪"]
    assert report["after_save"]["palette"]["primary"] == [18, 52, 86]
    resumed = launch(tmp_path, case="alias-win", phase="resume", resume=report["after"]["session_id"])
    assert selected(resumed["snapshot"])["definition"] == report["review"]["target"]["definition"]
    assert resumed["theme_before"]["palette"] == report["after_save"]["palette"]


@pytest.mark.parametrize("case", ["startup-alpha", "startup-ansi"])
def test_alpha_and_terminal_relative_registered_colors_are_reported_unsupported(tmp_path, case):
    report = launch(tmp_path, case=case)
    assert not any(row["name"] == "unsupported-color" for row in report["snapshot"]["rows"])
    diagnostic, = [row for row in report["snapshot"]["invalid"] if row["name"] == "unsupported-color"]
    assert diagnostic["reason"]
    assert selected(report["snapshot"])["palette"] == report["theme_before"]["palette"]
    assert report["theme_before"] == report["theme_after"]
    assert not report["writes"] and not report["requests"]


def test_unchanged_selection_is_saved_without_inventing_a_refresh(tmp_path):
    report = launch(tmp_path, case="select-current")
    assert_device_only_save(report)
    assert report["after_save"]["palette"] == report["theme_before"]["palette"]
    assert report["result"]["runtime"]["refresh_requested"] is False


def test_unrepresentable_active_theme_can_be_replaced_without_fabricating_initial_colors(tmp_path):
    report = launch(tmp_path, case="active-alpha")
    selection = report["snapshot"]["selection"]
    assert selection["effective_name"] == "unsupported-color"
    assert selection["effective_row_id"] is None and selection["effective_reason"]
    assert report["theme_before"]["palette"] is None
    assert_device_only_save(report)
    assert report["after_save"]["effective_name"] == "matrix"


def test_registered_unsupported_name_can_be_explicitly_repaired_with_reviewed_custom_definition(tmp_path):
    report = launch(tmp_path, case="repair-alpha")
    before = report["snapshot"]
    assert before["selection"]["effective_row_id"] is None
    assert before["registered_definitions"]["unsupported-color"] == before["selection"]["effective_definition"]
    assert_device_only_save(report)
    assert report["review"]["overwrite"]["registered"]
    assert report["review"]["overwrite"]["previous_definition"] == before["selection"]["effective_definition"]
    assert report["after_save"]["effective_name"] == "unsupported-color"
    assert report["after_save"]["palette"]["primary"] == [18, 52, 86]


def test_empty_historical_theme_name_remains_inspectable_and_explicitly_replaceable(tmp_path):
    report = launch(tmp_path, case="empty-selection")
    assert report["snapshot"]["selection"]["saved_name"] == ""
    assert report["snapshot"]["selection"]["saved_definition"] is None
    assert selected(report["snapshot"])["name"] == "textual-dark"
    assert_device_only_save(report)
    assert report["after_save"]["effective_name"] == "matrix"


def test_removed_light_builtin_is_not_claimed_as_durable_reconstructable_definition(tmp_path):
    report = launch(tmp_path, case="startup-light")
    assert report["snapshot"]["selection"]["saved_name"] == "textual-light"
    assert report["snapshot"]["selection"]["saved_definition"] is None
    assert selected(report["snapshot"])["name"] == "textual-dark"
    assert not report["writes"] and not report["requests"]
