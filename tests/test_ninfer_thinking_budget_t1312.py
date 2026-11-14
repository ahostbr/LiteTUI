"""T1312: the NInfer thinking budget. At N thinking tokens the engine closes the thinking and goes on to the answer
(ninfer-all docs/serving.md:1160-1203). LiteTUI sends `thinking_budget` as a top-level request field on NInfer only, and
never 0 (the engine answers 400 to a value < 1)."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from _owned_seat import conversation_id, owned_session
from textual.app import App

from litetui import settings_scope
from litetui.settings import Settings
from litetui.settings_runtime import apply_saved_result
from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui.settings_service import SettingsService
from litetui.turn_engine import TurnEngine

SRC = Path(__file__).resolve().parents[1] / "src" / "litetui"


def _chat(**over):
    args = dict(model_id="m", messages=[{"role": "user", "content": "hi"}], tools_enabled=False,
                max_tokens_tools=111, max_tokens_chat=222, request_overrides={}, thinking_level=None, tools=None)
    args.update(over)
    return TurnEngine.chat_request(**args)


def _compact(**over):
    args = dict(model_id="m", messages=[{"role": "user", "content": "sum"}], max_tokens=333, thinking_level="low",
                tools_enabled=False, tools=None)
    args.update(over)
    return TurnEngine.compact_request(**args)


@pytest.mark.parametrize("build", [_chat, _compact])
def test_ninfer_with_a_budget_sends_it_in_the_request_body(build):
    body = build(backend_name="ninfer", thinking_budget=2048)["extra_body"]
    assert body["thinking_budget"] == 2048
    assert body["return_progress"] is True  # the neighbouring NInfer field is untouched


@pytest.mark.parametrize("build", [_chat, _compact])
@pytest.mark.parametrize("budget", [None, 0, -5])
def test_no_budget_means_no_key_at_all(build, budget):
    """0 is OFF, never sent: the engine rejects a value < 1 with a 400."""
    assert "thinking_budget" not in build(backend_name="ninfer", thinking_budget=budget)["extra_body"]
    assert "thinking_budget" not in build(backend_name="ninfer")["extra_body"]


@pytest.mark.parametrize("build", [_chat, _compact])
@pytest.mark.parametrize("backend", ["lmstudio", "strata", "llama", "codex", ""])
def test_a_budget_is_never_sent_to_another_backend(build, backend):
    """Control for the NInfer arm: the same setting, the same call, a different backend."""
    kwargs = build(backend_name=backend, thinking_budget=2048)
    assert "thinking_budget" not in (kwargs.get("extra_body") or {})
    assert "thinking_budget" not in kwargs


def test_the_field_is_a_per_request_device_setting_not_an_engine_launch_flag():
    spec = settings_scope.SETTING_SPECS["ninfer_thinking_budget"]
    assert spec.scope == settings_scope.SettingScope.DEVICE
    assert spec.apply_timing == "immediate"  # not 'restart': it rides each request
    assert "ninfer_thinking_budget" not in settings_scope._ENGINE_LAUNCH
    assert "ninfer_thinking_budget" not in settings_scope._RESTART
    assert Settings().ninfer_thinking_budget is None


def test_every_turn_engine_call_in_app_passes_the_setting():
    """The wiring: a call site that forgets the argument silently sends no budget."""
    text = (SRC / "app.py").read_text(encoding="utf-8")
    calls = len(re.findall(r"TurnEngine\.(?:chat_request|compact_request)\(", text))
    wired = len(re.findall(r"thinking_budget=self\.settings\.ninfer_thinking_budget", text))
    assert calls == 3 and wired == calls


class BoundHost(App):
    backend = SimpleNamespace(name="lmstudio")
    model_id = "test-model"

    def __init__(self, root, cid):
        super().__init__()
        self._agent_session = owned_session(root)
        self.service = SettingsService(root, agent_session=self._agent_session)
        self.cid = cid
        self.settings = self.service.create_conversation(cid).effective

    def on_mount(self):
        self.push_screen(SettingsScreen(
            self.settings,
            snapshot_provider=lambda: self.service.snapshot(self.cid),
            save_patch=lambda changes, rev: self.service.save_patch(self.cid, changes, rev),
            runtime_apply=lambda target, result: apply_saved_result(self, target, result)))


@pytest.mark.asyncio
@pytest.mark.parametrize("typed, saved", [("2048", 2048), ("", None), ("0", 0)])
async def test_the_settings_screen_has_a_control_and_save_stores_it(tmp_path, typed, saved):
    cid = conversation_id("a")
    app = BoundHost(tmp_path, cid)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        control = body.query_one("#f-ninfer_thinking_budget")  # raises when there is no control: a dead Save button
        control.value = typed
        body.action_save()
        await pilot.pause()
        assert not app.screen.query(SettingsBody), "Save did not close the dialog"
        assert app.service.snapshot(cid).saved.ninfer_thinking_budget == saved
