"""The native /settings form is the real Textual settings screen, read and saved as data."""
from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest
from textual.app import App

from litetui import native_runtime_controls, native_settings_form
from litetui.settings_runtime import apply_saved_result
from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui.settings_service import SettingsService


class Host(App):
    backend = SimpleNamespace(name="lmstudio")
    model_id = "test-model"

    def __init__(self, service, cid):
        super().__init__()
        self.service, self.convo_id = service, cid
        self.settings = service.create_conversation(cid).effective

    def snapshot(self):
        return self.service.snapshot(self.convo_id)

    def dialog(self):
        cid = self.convo_id
        bindings = dict(snapshot_provider=lambda: self.service.snapshot(cid),
                        save_patch=lambda changes, rev: self.service.save_patch(cid, changes, rev),
                        runtime_apply=lambda target, result: apply_saved_result(self, target, result))
        return (partial(SettingsBody, self.settings, **bindings),
                partial(SettingsScreen, self.settings, **bindings), lambda result: None)


@pytest.fixture
def host(tmp_path, monkeypatch):
    import uuid

    from litetui import settings as settings_mod
    from litetui.agent_launch_context import ordinary

    with ordinary(tmp_path, settings_mod.Settings(default_model="test-model")) as session:
        app = Host(SettingsService(tmp_path, agent_session=session), str(uuid.uuid4()))
        yield from _bound(app, monkeypatch)


def _bound(app, monkeypatch):
    from litetui import gpu_gate

    # No hardware probe in tests; the NInfer tab is hardware-gated (T893).
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    monkeypatch.setattr("litetui.plugins.settings_ui.settings_dialog", lambda a: a.dialog())
    # Identity and owned-session checks are the backend catalog's, covered by its own suites.
    monkeypatch.setattr(native_settings_form, "_check", lambda *_: None)
    yield app


def controls(form):
    found = {}

    def visit(items):
        for item in items:
            if item["kind"] in {"line", "row"}:
                visit(item["items"])
            elif "id" in item and item["kind"] in {"input", "switch", "select", "textarea"}:
                found[item["id"]] = item
    for tab in form["tabs"]:
        visit(tab["items"])
    return found


CMD = {"type": native_settings_form.FORM, "version": 1, "session_id": "a"}


@pytest.mark.asyncio
async def test_form_is_the_mounted_screen_in_order_and_leaves_no_screen_behind(host):
    async with host.run_test(size=(140, 45)) as pilot:
        depth = len(host.screen_stack)
        form = await native_settings_form.dispatch(host, CMD)
        await pilot.pause()
        assert len(host.screen_stack) == depth
        # The same screen, mounted by hand: its tabs and every control id.
        screen = SettingsScreen(host.settings)
        await host.push_screen(screen)
        await pilot.pause()
        from textual.widgets import Input, Select, Switch, TabbedContent, TabPane, TextArea
        tabbed = screen.query_one(TabbedContent)
        labels = [tabbed.get_tab(p.id).label.plain for p in tabbed.query(TabPane)]
        ids = {w.id for kind in (Input, Select, Switch, TextArea) for w in screen.query(kind)
               if w.id and w.id != "set-search" and not w.id.startswith("hook-")}
    assert form["title"] == "Settings"
    assert form["subtitle"] == "Esc cancels · Ctrl+S saves · ←/→ or click to change tab"
    assert [t["label"] for t in form["tabs"]] == labels
    assert labels == ["Model", "Strata", "Xolotl", "Voice", "Generation", "Agent loop", "Compaction",
                      "Capabilities", "Hooks", "Themes", "Interface"]
    assert set(controls(form)) == ids
    model = form["tabs"][0]["items"]
    assert model[0]["kind"] == "section" and model[0]["label"].startswith("▾ Connection & default model")
    # Python's own search text, so the frontend's search matches what search_settings matches.
    from litetui.settings_ui_model import SETTINGS_SECTIONS, _search_text
    spec = next(sec for sec in SETTINGS_SECTIONS if sec.section_id == "model-connection")
    assert model[0]["search"] == _search_text(spec)
    assert set(model[0]["fields"]) == {f.name for f in spec.fields}
    assert form["search_status"] == ""  # empty until the first search, as on screen
    rows = [i for i in model if i["kind"] == "row"]
    assert rows[0]["items"][0] == {"kind": "label", "text": "Default model", "inline": False,
                                   "section": "model-connection"}
    assert [i["kind"] for i in rows[0]["items"]] == ["label", "select", "text"]
    assert [b["label"] for b in form["buttons"]][:3] == ["Save", "Cancel", "Restore defaults"]
    assert len(form["form"]) == 64


@pytest.mark.asyncio
async def test_secrets_never_leave_the_process(host):
    host.settings.anthropic_api_key = "sk-secret-value-123"
    async with host.run_test(size=(140, 45)):
        form = await native_settings_form.dispatch(host, CMD)
    item = controls(form)["f-anthropic_api_key"]
    assert item["password"] and item["set"] is True and "value" not in item
    assert "sk-secret-value-123" not in json.dumps(form)


@pytest.mark.asyncio
async def test_save_presses_pythons_own_save_and_reads_back(host):
    async with host.run_test(size=(140, 45)):
        form = await native_settings_form.dispatch(host, CMD)
        result = await native_settings_form.dispatch(host, {
            **CMD, "type": native_settings_form.SAVE, "form": form["form"],
            "values": {"f-lm_host": "http://127.0.0.1:9999", "f-autoscroll": False}})
    assert result["status"] == "saved", result
    saved = host.snapshot().saved
    assert saved.lm_host == "http://127.0.0.1:9999" and saved.autoscroll is False
    fresh = controls(result)
    # The readback is what Python's reopened dialog shows: effective values. autoscroll applies at
    # once; lm_host is saved but applies on reconnect, so Python still shows the running host.
    assert fresh["f-autoscroll"]["value"] is False
    assert fresh["f-lm_host"]["value"] == host.settings.lm_host


@pytest.mark.asyncio
async def test_invalid_value_is_refused_with_pythons_message_and_nothing_saved(host):
    async with host.run_test(size=(140, 45)):
        before = host.snapshot().revisions
        form = await native_settings_form.dispatch(host, CMD)
        result = await native_settings_form.dispatch(host, {
            **CMD, "type": native_settings_form.SAVE, "form": form["form"],
            "values": {"f-tool_iterations": "lots"}})
    assert result["status"] == "refused"
    assert "tool_iterations" in result["message"] and "Cannot save" in result["message"]
    assert host.snapshot().revisions == before


@pytest.mark.asyncio
async def test_stale_form_unknown_control_and_bad_values_change_nothing(host):
    async with host.run_test(size=(140, 45)):
        before = host.snapshot().revisions
        form = await native_settings_form.dispatch(host, CMD)
        save = {**CMD, "type": native_settings_form.SAVE, "form": "0" * 64,
                "values": {"f-lm_host": "http://127.0.0.1:9999"}}
        assert (await native_settings_form.dispatch(host, save))["status"] == "conflict"
        for values in ({"f-no_such": "x"}, {"f-autoscroll": "yes"}, {"f-theme_name": "not-a-theme"}, {}):
            with pytest.raises(ValueError):
                await native_settings_form.dispatch(host, {**save, "form": form["form"], "values": values})
    assert host.snapshot().revisions == before


@pytest.mark.asyncio
async def test_reviewed_runtime_fields_are_read_only_here(host, monkeypatch):
    monkeypatch.setattr(native_runtime_controls, "enabled", lambda app: True)
    monkeypatch.setattr(native_runtime_controls, "reviewed_fields", lambda app: {"tools_enabled"})
    async with host.run_test(size=(140, 45)):
        form = await native_settings_form.dispatch(host, CMD)
        item = controls(form)["f-tools_enabled"]
        assert item["disabled"] and item["reason"] == native_settings_form.REVIEWED
        with pytest.raises(ValueError, match="reviewed control"):
            await native_settings_form.dispatch(host, {
                **CMD, "type": native_settings_form.SAVE, "form": form["form"],
                "values": {"f-tools_enabled": True}})
