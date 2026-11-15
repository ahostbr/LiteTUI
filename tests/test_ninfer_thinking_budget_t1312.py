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


def test_the_answer_gets_its_own_room_on_top_of_the_budget_chat():
    """NInfer counts thinking INSIDE max_tokens: a 16384 budget under a 16384 max_tokens_tools let thinking eat the
    whole output and the reply ended with no answer (Ryan's Edge0 request #2: out = 16,384 = max_tokens)."""
    k = _chat(backend_name="ninfer", thinking_budget=16384, tools_enabled=True, tools=[], max_tokens_tools=16384)
    assert k["extra_body"]["thinking_budget"] == 16384
    assert k["max_tokens"] == 32768
    # The chat (tools off) value is the one raised when tools are off: the room follows the same toggle.
    assert _chat(backend_name="ninfer", thinking_budget=2048, tools_enabled=False)["max_tokens"] == 222 + 2048


def test_the_answer_gets_its_own_room_on_top_of_the_budget_compaction():
    k = _compact(backend_name="ninfer", thinking_budget=2048, max_tokens=333)
    assert k["extra_body"]["thinking_budget"] == 2048 and k["max_tokens"] == 333 + 2048


def test_a_per_model_max_tokens_override_is_the_base_that_is_raised():
    k = _chat(backend_name="ninfer", thinking_budget=1000, request_overrides={"max_tokens": 4000})
    assert k["max_tokens"] == 5000


@pytest.mark.parametrize("build", [_chat, _compact])
@pytest.mark.parametrize("budget", [None, 0, -5])
def test_no_budget_means_no_key_and_no_extra_room(build, budget):
    """0 is OFF, never sent: the engine rejects a value < 1 with a 400."""
    base = build(backend_name="ninfer")
    got = build(backend_name="ninfer", thinking_budget=budget)
    assert "thinking_budget" not in got["extra_body"]
    assert got["max_tokens"] == base["max_tokens"] == (222 if build is _chat else 333)


@pytest.mark.parametrize("build", [_chat, _compact])
@pytest.mark.parametrize("backend", ["lmstudio", "llama", "codex", ""])  # strata/xolotl: tests/test_strata_thinking_budget_t1318.py
def test_another_backend_gets_neither_the_key_nor_the_room(build, backend):
    """Control for the NInfer arm: the same setting, the same call, a different backend."""
    kwargs = build(backend_name=backend, thinking_budget=2048)
    assert "thinking_budget" not in (kwargs.get("extra_body") or {})
    assert "thinking_budget" not in kwargs
    assert kwargs["max_tokens"] == (222 if build is _chat else 333)


@pytest.mark.parametrize("off", [
    dict(thinking_level="off"),
    dict(request_overrides={"enable_thinking": False}),
])
def test_a_chat_request_with_thinking_off_gets_neither_the_key_nor_the_room(off):
    k = _chat(backend_name="ninfer", thinking_budget=2048, **off)
    assert "thinking_budget" not in k["extra_body"]
    assert k["max_tokens"] == 222


def test_thinking_on_is_the_control_for_the_off_arms():
    """Same call as the off arms with thinking left alone: the key and the room appear."""
    for on in (dict(thinking_level="low"), dict(thinking_level=None), dict(request_overrides={"enable_thinking": True})):
        k = _chat(backend_name="ninfer", thinking_budget=2048, **on)
        assert k["extra_body"]["thinking_budget"] == 2048 and k["max_tokens"] == 222 + 2048


def test_a_compaction_with_thinking_off_gets_neither():
    k = _compact(backend_name="ninfer", thinking_budget=2048, thinking_level="off")
    assert "thinking_budget" not in k["extra_body"] and k["max_tokens"] == 333


def test_a_compaction_pinned_to_the_chat_effort_follows_that_effort():
    """The cache-friendly compaction is re-pinned to the chat turn's effort after it is built, so the budget and the
    room follow THAT effort, not compact_thinking_level."""
    off = _compact(backend_name="ninfer", thinking_budget=2048, thinking_level="low", pinned_effort="none")
    assert "thinking_budget" not in off["extra_body"] and off["max_tokens"] == 333
    on = _compact(backend_name="ninfer", thinking_budget=2048, thinking_level="off", pinned_effort=None)
    assert on["extra_body"]["thinking_budget"] == 2048 and on["max_tokens"] == 333 + 2048
    on2 = _compact(backend_name="ninfer", thinking_budget=2048, thinking_level="off", pinned_effort="low")
    assert on2["max_tokens"] == 333 + 2048


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
    wired = len(re.findall(r"thinking_budget=TurnEngine\.thinking_budget_for\(self\.settings, self\.backend\.name\)", text))
    pinned = len(re.findall(r"pinned_effort=\(getattr\(self, \"_last_chat_effort\"", text))
    assert calls == 3 and wired == calls
    assert pinned == 2, "both compaction requests must pass the chat effort they are pinned to"


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


# ── the room a server names is a TOTAL: it still wins over the raised value ──────────────

@pytest.mark.asyncio
async def test_a_servers_named_room_still_caps_a_compaction_raised_by_the_budget(monkeypatch):
    """Replay of tests/test_compact_reactive_retry.py's strata server on NInfer with a 1000-token budget. The first
    request is 24000 + 1000; the refusal names the room and every later request starts from it. Before the clamp
    was moved after the raise, the raise was applied on top of the learned cap, the fallback request raised an
    already-raised value, and each round was refused once more."""
    from test_compact_reactive_retry import (RECORDED_PROMPT_TOKENS, WINDOW, _conversation_like_the_recording,
                                             _replay_app, _run)
    app, notes, log, served = _replay_app(monkeypatch, _conversation_like_the_recording(), "strata")
    # The test app's backend cannot be renamed (the owned-agent authority check refuses it), so the request
    # builder is told it is NInfer with a 1000-token budget (the app now reads the budget per backend); the app's own clamp and fallback logic is what is under test.
    build = TurnEngine.compact_request
    monkeypatch.setattr(TurnEngine, "compact_request",
                        staticmethod(lambda **kw: build(**{**kw, "backend_name": "ninfer", "thinking_budget": 1000})))
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert served["rounds"] == 9, notes
    first = 24000 + 1000
    assert log["refused"][0] == (1, first)
    expected, cap = [first, WINDOW - RECORDED_PROMPT_TOKENS[0]], WINDOW - RECORDED_PROMPT_TOKENS[0]
    for prompt in RECORDED_PROMPT_TOKENS[1:]:
        if prompt + cap > WINDOW:
            expected.append(cap)
            cap = WINDOW - prompt
        expected.append(cap)
    assert log["compaction"] == expected, (log["compaction"], expected)
