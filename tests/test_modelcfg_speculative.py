"""Textual /modelcfg speculation round trips; no native loads."""
from __future__ import annotations

import pytest
from test_llama_speculative import MODES, gguf
from test_model_screen import _app
from textual.widgets import Input, Select
from textual.widgets.select import InvalidSelectValueError

from litetui import llm_backend as b
from litetui.plugins.model_switch import ModelConfigBody, ModelConfigScreen


@pytest.fixture
def app(tmp_path, monkeypatch):
    a = _app(monkeypatch)
    monkeypatch.setattr(b, "configured_spec_types", lambda s: MODES)
    path = gguf(tmp_path / "normal-name.gguf")
    a.model_rows = {"m": b.ModelRow("m", str(path), "custom", nextn_predict_layers=1)}
    return a


@pytest.mark.asyncio
async def test_selector_saves_mtp_not_a_coerced_number(app):
    async with app.run_test(size=(120, 45)) as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        app.screen.query_one("#ld-spec_type", Select).value = "draft-mtp"
        app.screen.query_one("#ld-draft_max", Input).value = "  "
        app.screen.query_one("#ld-draft_model", Input).value = "  "
        app.screen.action_apply()
        await pilot.pause()
    assert app.settings.llama_load_settings["m"] == {"spec_type": "draft-mtp"}
    assert app.backend.applied == [("m", {"spec_type": "draft-mtp"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["binary", "gguf"])
async def test_mtp_is_not_offered_without_both_evidence_sources(app, monkeypatch, missing):
    if missing == "binary":
        monkeypatch.setattr(b, "configured_spec_types", lambda s: frozenset({"none", "draft-simple"}))
    else:
        row = app.model_rows["m"]
        app.model_rows["m"] = b.ModelRow("m", row.path, "custom")
    async with app.run_test(size=(120, 45)) as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        selector = app.screen.query_one("#ld-spec_type", Select)
        with pytest.raises(InvalidSelectValueError):
            selector.value = "draft-mtp"
        selector.value = "none"   # the recovery path remains offered


@pytest.mark.asyncio
async def test_preset_cannot_bypass_speculation_validation(app):
    app.settings.llama_presets = {"stale": {"load": {"spec_type": "draft-simple"}}}
    async with app.run_test() as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        app.screen.query_one("#mc-preset-apply", Select).value = "stale"
        app.screen.query_one("#mc-preset-name", Input).value = "must-not-save"
        app.screen.action_apply()
        await pilot.pause()
        assert app.screen.query(ModelConfigBody)
        assert "m:" in str(app.screen.query_one("#mc-error").render())
    assert "m" not in app.settings.llama_load_settings
    assert "must-not-save" not in app.settings.llama_presets
    assert app.backend.applied == []


@pytest.mark.asyncio
async def test_ui_preflight_rereads_file_before_save(app):
    async with app.run_test() as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        app.screen.query_one("#ld-spec_type", Select).value = "draft-mtp"
        from pathlib import Path
        gguf(Path(app.model_rows["m"].path), layers=0)
        app.screen.action_apply()
        await pilot.pause()
        assert app.screen.query(ModelConfigBody)
    assert "m" not in app.settings.llama_load_settings
    assert app.backend.applied == []


@pytest.mark.asyncio
async def test_stale_mode_survives_open_but_can_be_explicitly_turned_off(app, monkeypatch):
    app.settings.llama_load_settings = {"m": {"spec_type": "draft-mtp", "draft_model": "stale"}}
    monkeypatch.setattr(b, "configured_spec_types", lambda s: frozenset({"none"}))
    async with app.run_test() as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        selector = app.screen.query_one("#ld-spec_type", Select)
        assert selector.value == "draft-mtp"
        selector.value = "none"
        app.screen.action_apply()
        await pilot.pause()
    assert app.backend.applied[0][1]["spec_type"] == "none"


@pytest.mark.asyncio
async def test_off_remains_available_with_unknown_binary(app, monkeypatch):
    monkeypatch.setattr(b, "configured_flags", lambda s: frozenset())
    monkeypatch.setattr(b, "configured_spec_types", lambda s: frozenset())
    async with app.run_test() as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        app.screen.query_one("#ld-spec_type", Select).value = "none"
        app.screen.action_apply()
        await pilot.pause()
    assert app.backend.applied == [("m", {"spec_type": "none"})]



@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [False, True])
async def test_off_ignores_malformed_stale_tuning(app, preset):
    app.settings.llama_load_settings = {"m": {"draft_max": "bad"}}
    app.settings.llama_presets = {"off": {"load": {"spec_type": "none"}}}
    async with app.run_test() as pilot:
        app.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        if preset:
            app.screen.query_one("#mc-preset-apply", Select).value = "off"
        else:
            app.screen.query_one("#ld-spec_type", Select).value = "none"
        app.screen.action_apply()
        await pilot.pause()
    assert app.backend.applied[0][1]["spec_type"] == "none"
