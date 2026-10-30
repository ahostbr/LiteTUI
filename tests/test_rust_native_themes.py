"""Owned theme editing through real settings/registry APIs, external I/O forbidden."""
from __future__ import annotations

import copy
import json

import pytest
import pytest_asyncio
from test_rust_backend_transition import disk
from test_rust_backend_transition import switch_app as _controlled_app
from textual.color import Color
from textual.theme import Theme

from litetui import (
    gui_rpc,
    native_engine,
    native_themes,
    settings_runtime,
    settings_service,
    theme_palette,
    themes,
)
from litetui.plugins import themes_plugin

switch_app = _controlled_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def theme_app(switch_app):
    app = switch_app
    with app._context():
        themes_plugin._activate(app)
        yield app


def common(app):
    return {"version": 1, "session_id": app.convo_id,
            "agent_id": app._agent_session.authority.agent_id,
            "backend": app.backend.name, "model": app.model_id}


def tokens(color="#202020", **extras):
    return {**{key: color for key in themes.THEME_TOKENS}, **extras}


def draft(app, *, name=None, values=None, select="matrix"):
    state = native_themes.snapshot(app, common(app))
    result = {**common(app), "snapshot": state["snapshot"], "revisions": state["revisions"]}
    if name is not None:
        return {**result, "action": "custom", "name": name, "tokens": values if values is not None else tokens()}
    row = next(row for row in state["rows"] if row["name"] == select)
    return {**result, "action": "select", "row_id": row["row_id"]}


def review(app, **kwargs):
    reviewed = native_themes.validate(app, draft(app, **kwargs))
    request = {key: reviewed[key] for key in (*native_themes.COMMON, "snapshot", "revisions", "review", "action_id")}
    return request, reviewed


def write_saved(app, customs):
    service = settings_runtime.service_for(app)
    snap = service.snapshot(app.convo_id, read_only=True)
    result = service.save_patch(app.convo_id, [settings_service.SettingChange("custom_themes", customs, "device")], snap.revisions)
    assert result.fully_saved


async def test_snapshot_review_are_pure_and_source_ordered(theme_app):
    app = theme_app
    before = disk(app)
    registry = list(app.available_themes)
    state = native_themes.snapshot(app, common(app))
    assert [row["name"] for row in state["rows"]] == [name for name in registry if app.available_themes[name].dark]
    assert len({row["row_id"] for row in state["rows"]}) == len(state["rows"])
    for row in state["rows"]:
        assert set(row["palette"]) == set(theme_palette.PALETTE_KEYS)
        assert [token["key"] for token in row["tokens"]] == list(themes.THEME_FORM_TOKENS)
    review(app)
    assert disk(app) == before and list(app.available_themes) == registry


async def test_select_saves_device_only_and_independent_readback(theme_app):
    app = theme_app
    before, history, authority = disk(app), copy.deepcopy(app.conversation), app._agent_session.authority
    request, reviewed = review(app)
    result = await gui_rpc.async_dispatch(app, {"type": "gui.themes.native_save", **request})
    assert result["status"] == "saved", result
    assert result["selection"]["saved_definition"] == reviewed["target"]["definition"]
    assert result["selection"]["effective_definition"] == reviewed["target"]["definition"]
    assert result["persistence"][0]["scope"] == "device"
    assert {path for path, data in disk(app).items() if before.get(path) != data} == {
        str(app._agent_session.store.data_root / "settings.json")}
    assert app.conversation == history and app._agent_session.authority == authority
    assert native_themes.snapshot(app, common(app))["selection"] == result["selection"]
    assert native_themes.save(app, request)["status"] == "refused"


@pytest.mark.parametrize("name", ["my-theme", "matrix", "textual-dark", "textual-light", "Theme 雪"])
async def test_custom_create_overwrite_and_canonical_device_restart(theme_app, name):
    app = theme_app
    request, reviewed = review(app, name=name)
    result = native_themes.save(app, request)
    assert result["status"] == "saved", result
    saved = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True).saved
    rebuilt = themes.theme_from_tokens(name, saved.custom_themes[name])
    assert theme_palette.definition_digest(rebuilt) == reviewed["target"]["definition"]
    state = native_themes.snapshot(app, common(app))
    assert len([row for row in state["rows"] if row["name"] == name]) == 1
    assert state["selection"]["saved_definition"] == state["selection"]["effective_definition"]


async def test_same_name_edit_requests_css_and_footer_refresh(theme_app, monkeypatch):
    app = theme_app
    assert native_themes.save(app, review(app, name="mine")[0])["status"] == "saved"
    refreshes = []
    monkeypatch.setattr(app, "_refresh_ctx_label", lambda: refreshes.append(True))
    before = app._css_update_count
    request, reviewed = review(app, name="mine", values=tokens("#332211"))
    result = native_themes.save(app, request)
    assert result["status"] == "saved", result
    assert result["runtime"]["refresh_requested"]
    assert app._css_update_count > before and refreshes
    assert theme_palette.definition_digest(app.current_theme) == reviewed["target"]["definition"]


async def test_saved_map_merge_preserves_disk_only_and_invalid_entries(theme_app):
    app = theme_app
    originals = {"external": tokens("#554433"), "invalid": {"primary": "broken"}}
    write_saved(app, originals)
    assert app.settings.custom_themes == {}
    assert native_themes.save(app, review(app, name="new")[0])["status"] == "saved"
    saved = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True).saved.custom_themes
    assert {key: saved[key] for key in originals} == originals


@pytest.mark.parametrize("winning", [False, True])
async def test_trimmed_alias_registration_winner_checked_before_save(theme_app, winning):
    app = theme_app
    entries = [("Theme 雪", tokens("#111111")), (" Theme 雪 ", tokens("#222222"))]
    if winning:
        entries.reverse()
    write_saved(app, dict(entries))
    before = disk(app)
    command = draft(app, name="Theme 雪", values=tokens("#333333"))
    if not winning:
        with pytest.raises(ValueError, match="alias"):
            native_themes.validate(app, command)
        assert disk(app) == before
    else:
        request, reviewed = review(app, name="Theme 雪", values=tokens("#333333"))
        result = native_themes.save(app, request)
        assert result["status"] == "saved", result
        assert result["selection"]["saved_definition"] == reviewed["target"]["definition"]


@pytest.mark.parametrize("change", ["core", "empty", "comma", "unknown", "name_bytes", "token_bytes"])
async def test_invalid_form_refuses_before_any_effect(theme_app, change):
    app = theme_app
    command = draft(app, name="valid")
    if change == "core":
        command["tokens"]["primary"] = "red"
    elif change == "empty":
        command["name"] = " "
    elif change == "comma":
        command["name"] = "a,b"
    elif change == "unknown":
        command["tokens"]["unknown"] = "#111111"
    elif change == "name_bytes":
        command["name"] = "雪" * 86
    else:
        command["tokens"]["tool-text"] = "a" * 257
    before = disk(app)
    with pytest.raises((ValueError, TypeError)):
        native_themes.validate(app, command)
    assert disk(app) == before


async def test_optional_invalid_custom_tokens_and_registered_nonhex_are_distinct(theme_app):
    app = theme_app
    raw = tokens(**{"tool-text": "red", "footer-background": "invalid"})
    reviewed = native_themes.validate(app, draft(app, name="optional", values=raw))
    rows = {row["key"]: row for row in reviewed["target"]["tokens"]}
    assert rows["tool-text"]["raw"] == "red" and rows["tool-text"]["source"] == "derived"
    assert rows["footer-background"]["resolved"] is None and rows["footer-background"]["source"] == "omitted"
    app.register_theme(Theme(name="registered", primary="green", variables={"tool-text": "red"}))
    state = native_themes.snapshot(app, common(app))
    row = next(row for row in state["rows"] if row["name"] == "registered")
    token = next(token for token in row["tokens"] if token["key"] == "tool-text")
    assert token["resolved"] == "#ff0000" and token["source"] == "explicit"
    assert row["palette"]["tool_text"] == [255, 0, 0]


async def test_footer_palette_uses_actual_generated_strip_background():
    theme = themes.ALL_THEMES["ash"]
    generated = theme.to_color_system().generate()
    palette = theme_palette.resolved_colors(theme)
    assert palette["footer_background"] == Color.parse(generated["footer-background"]).rgb
    assert palette["footer_background"] != palette["background"]


@pytest.mark.parametrize("fault", ["global", "registry", "foreign", "held"])
async def test_stale_review_refuses_without_mutation(theme_app, monkeypatch, fault):
    app = theme_app
    request, _ = review(app)
    if fault == "global":
        write_saved(app, {"new-external": tokens()})
    elif fault == "registry":
        monkeypatch.setattr(app.available_themes["matrix"], "variables", {**app.available_themes["matrix"].variables, "tool-text": "#eeeeee"})
    elif fault == "foreign":
        request["agent_id"] = "0" * 32
    else:
        app._pending_input.append({"content": "held"})
    before = disk(app)
    result = native_themes.save(app, request)
    assert result["status"] == "refused" and not result["mutated"], result
    assert disk(app) == before


@pytest.mark.parametrize("after_write", [False, True])
async def test_device_write_failure_reports_exact_known_durable_outcome(theme_app, monkeypatch, after_write):
    app = theme_app
    request, reviewed = review(app, name="write-failure")
    write = settings_service._write

    def fault(*args):
        if after_write:
            write(*args)
        raise OSError("Controlled write failure")

    monkeypatch.setattr(settings_service, "_write", fault)
    result = native_themes.save(app, request)
    assert result["status"] == ("partial" if after_write else "refused"), result
    assert not result["persistence"][0]["saved"]
    assert result["runtime"]["status"] == "unchanged"
    assert not result["close_required"]
    if after_write:
        state = native_themes.snapshot(app, common(app))
        assert state["selection"]["saved_definition"] == reviewed["target"]["definition"]
        assert state["selection"]["effective_name"] != reviewed["target"]["name"]


async def test_registration_failure_keeps_chat_authority_and_proves_saved_custom(theme_app, monkeypatch):
    app = theme_app
    request, reviewed = review(app, name="cannot-register")
    monkeypatch.setattr(app, "register_theme", lambda _theme: (_ for _ in ()).throw(RuntimeError("Controlled registration failure")))
    result = native_themes.save(app, request)
    assert result["status"] == "partial" and not result["close_required"], result
    assert result["runtime"]["status"] == "failed"
    assert result["selection"]["saved_definition"] == reviewed["target"]["definition"]
    app._validate_owned_execution()


async def test_management_only_snapshot_is_pure_but_save_ineligible(theme_app):
    app = theme_app
    app._native_engine_state = native_engine.State(app.backend, session_id=app.convo_id,
        desired_model=app.model_id, session=app._agent_session,
        agent_id=app._agent_session.authority.agent_id, admission="management_only", effective_model=None)
    before = disk(app)
    result = await gui_rpc.async_dispatch(app, {"type": "gui.themes.native_snapshot", **common(app)})
    assert not result["eligible"] and result["reason"]
    assert result["selection"]["effective_name"] == app.current_theme.name
    assert disk(app) == before


async def test_later_theme_watch_does_not_overwrite_external_custom_map(theme_app):
    app = theme_app
    assert native_themes.save(app, review(app, name="mine")[0])["status"] == "saved"
    original = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True).saved.custom_themes
    changed = {**original, "later-external": tokens("#111122")}
    write_saved(app, changed)
    app.theme = "matrix"
    raw = json.loads((app._agent_session.store.data_root / "settings.json").read_text(encoding="utf-8"))
    assert raw["custom_themes"] == changed
    assert raw["theme_name"] == "matrix"


async def test_full_snapshot_envelope_budget_refuses_before_emission(theme_app, monkeypatch):
    app = theme_app
    cmd = common(app)
    result = native_themes.snapshot(app, cmd)
    limit = len(json.dumps(result).encode("utf-8"))
    monkeypatch.setattr(native_themes, "MAX_RESPONSE", limit + 20)
    before = disk(app)
    with pytest.raises(ValueError, match="wire budget"):
        native_themes.snapshot(app, {**cmd, "id": "x" * 100})
    assert disk(app) == before


async def test_oversized_save_id_refuses_before_device_write(theme_app):
    app = theme_app
    request, _ = review(app)
    before = disk(app)
    result = native_themes.save(app, {**request, "id": "x" * 4097})
    assert result["status"] == "refused" and not result["mutated"]
    assert disk(app) == before


@pytest.mark.parametrize("value", ["#ff000080", "ansi_default", "auto"])
async def test_registered_nonopaque_colors_are_reported_without_flattening(theme_app, value):
    app = theme_app
    candidate = Theme(name="unsupported", primary="#123456", variables={"thinking-text": value})
    app.register_theme(candidate)
    with pytest.raises(ValueError, match="opaque RGB"):
        theme_palette.resolved_colors(candidate)
    state = native_themes.snapshot(app, common(app))
    assert not any(row["name"] == "unsupported" for row in state["rows"])
    assert any(row["name"] == "unsupported" and "opaque RGB" in row["reason"] for row in state["invalid"])


async def test_unsupported_effective_palette_remains_recoverable(theme_app):
    app = theme_app
    app.register_theme(Theme(name="unsupported", primary="#123456", variables={"thinking-text": "#ff000080"}))
    app.settings.theme_name = "unsupported"
    app.theme = "unsupported"
    state = native_themes.snapshot(app, common(app))
    assert state["selection"]["effective_name"] == "unsupported"
    assert state["selection"]["effective_row_id"] is None and state["selection"]["effective_reason"]
    result = native_themes.save(app, review(app)[0])
    assert result["status"] == "saved" and result["selection"]["effective_name"] == "matrix"
    assert result["selection"]["effective_row_id"] and result["selection"]["effective_reason"] is None


async def test_unregistered_saved_light_name_has_no_durable_definition(theme_app):
    app = theme_app
    service = settings_runtime.service_for(app)
    before = service.snapshot(app.convo_id, read_only=True)
    service.save_patch(app.convo_id, [settings_service.SettingChange("theme_name", "textual-light", "device")], before.revisions)
    state = native_themes.snapshot(app, common(app))
    assert state["selection"]["saved_name"] == "textual-light"
    assert state["selection"]["saved_definition"] is None


async def test_unsupported_registered_name_can_be_explicitly_repaired(theme_app):
    app = theme_app
    original = Theme(name="repair", primary="#123456", variables={"thinking-text": "#ff000080"})
    app.register_theme(original)
    state = native_themes.snapshot(app, common(app))
    assert state["registered_definitions"]["repair"] == theme_palette.definition_digest(original)
    request, reviewed = review(app, name="repair")
    assert reviewed["overwrite"]["registered"] and reviewed["overwrite"]["previous_definition"] == state["registered_definitions"]["repair"]
    result = native_themes.save(app, request)
    assert result["status"] == "saved", result


async def test_empty_saved_choice_is_visible_and_repairable(theme_app):
    app = theme_app
    service = settings_runtime.service_for(app)
    before = service.snapshot(app.convo_id, read_only=True)
    service.save_patch(app.convo_id, [settings_service.SettingChange("theme_name", "", "device")], before.revisions)
    app.settings.theme_name = ""
    state = native_themes.snapshot(app, common(app))
    assert state["selection"]["saved_name"] == state["selection"]["requested_name"] == ""
    assert state["selection"]["effective_name"] == "textual-dark"
    assert native_themes.save(app, review(app)[0])["status"] == "saved"


async def test_combined_cached_and_disk_diagnostics_are_bounded(theme_app, monkeypatch):
    app = theme_app
    app.settings.custom_themes = {f"cached-{index}": {} for index in range(23)}
    write_saved(app, {f"saved-{index}": {} for index in range(23)})
    monkeypatch.setattr(native_themes, "MAX_ROWS", 40)
    before = disk(app)
    with pytest.raises(ValueError, match="diagnostics"):
        native_themes.snapshot(app, common(app))
    assert disk(app) == before
