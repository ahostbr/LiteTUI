"""/pause: the whole agent loop wrapped in `if not paused`.

RYAN, 2026-09-18 13:3x: "i want the entire agent loop wrapped in a if not
paused statement ... that i can toggle with /pause" and "add a onscreen button
also kinda like the tool cancel one".

One gate at the top of the stream loop covers every model round and a turn's
first round. Two doors - /pause and the button - run one body.
"""
import asyncio
from types import SimpleNamespace

import pytest

from litetui import app as app_mod, paths
from litetui.settings import Settings
from litetui.tool_policy import SHELL_POLICY
from litetui.widgets import AssistantMessage, PauseButton

from tests.test_card_summary import _app, _Resp, _Stream, _Chunk, _probe


def _agentic_app(monkeypatch, tmp_path, on_tool):
    """Two rounds: an answer + tool call, then the final answer. `on_tool` runs
    inside the tool between the rounds - where a pause lands mid-turn."""
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path)
    app = _app()
    app.settings = Settings(tools_enabled=True, tool_iterations=5,
                            autocompact_enabled=False, wake_after_compact=False,
                            clear_screen_after_compact=False)
    app.tools_enabled = True
    app.model_id = "fixture"
    app.available_models = ["fixture"]
    app.said = []
    app._system = lambda msg, *a, **k: app.said.append(str(msg))

    async def ready():
        pass
    app._ensure_chat_ready = ready
    app.plugins.add_tool(
        "test",
        {"type": "function", "function": {"name": "probe", "description": "t",
                                          "parameters": {"type": "object", "properties": {}}}},
        lambda args: on_tool() or "ran", policy=SHELL_POLICY)
    rounds = iter([
        _Stream([_Chunk(content="Looking first.")] + _probe(1)),
        _Stream([_Chunk(content="Done.")]),
    ])
    app.creates = 0

    async def create(**kw):
        if kw.get("purpose", "turn") != "turn":
            return _Resp("one line")
        app.creates += 1
        return next(rounds)
    monkeypatch.setattr(app_mod.model_transport, "for_app",
                        lambda _a: SimpleNamespace(create=create))
    return app


async def _spin(app, pilot, frames):
    for _ in range(frames):
        await pilot.pause()
        if not app._chat_running():
            return


@pytest.mark.asyncio
async def test_a_pause_mid_turn_holds_the_next_round_until_resumed(monkeypatch, tmp_path):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: app.set_paused(True))
    async with app.run_test(size=(100, 40)) as pilot:
        app._append({"role": "user", "content": "go"})
        app._stream()
        await _spin(app, pilot, 60)          # well past where round 2 would start
        assert app.creates == 1, "the second model round went out while paused"
        assert app._chat_running(), "the turn must stay alive, holding - not end"
        assert any("paused" in s for s in app.said)

        app.set_paused(False)
        await _spin(app, pilot, 200)
        for _ in range(10):
            await pilot.pause()
        assert app.creates == 2
        assert [c.answer_text for c in app.query(AssistantMessage)] == ["Looking first.", "Done."]
        assert app.said[-1] == "[resumed]"


@pytest.mark.asyncio
async def test_esc_while_paused_still_ends_the_turn(monkeypatch, tmp_path):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: app.set_paused(True))
    async with app.run_test(size=(100, 40)) as pilot:
        app._append({"role": "user", "content": "go"})
        app._stream()
        await _spin(app, pilot, 60)
        assert app.creates == 1 and app._chat_running()
        app._stop_requested = True             # what Esc sets
        await _spin(app, pilot, 200)
        assert not app._chat_running(), "a stop while paused must end the turn, not wait for resume"
        assert app.creates == 1
        assert app.paused, "stopping the turn does not un-pause the seat"


@pytest.mark.asyncio
async def test_a_turn_started_while_paused_waits_before_its_first_call(monkeypatch, tmp_path):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: None)
    async with app.run_test(size=(100, 40)) as pilot:
        app.set_paused(True)
        app._append({"role": "user", "content": "go"})
        app._stream()
        await _spin(app, pilot, 40)
        assert app.creates == 0, "a paused seat must not call the model at all"
        assert app._chat_running()
        app.set_paused(False)
        await _spin(app, pilot, 200)
        assert app.creates == 2


class TestTwoDoors:
    def test_the_command_toggles_and_announces(self):
        app = _app()
        said = []
        app._system = lambda msg, *a, **k: said.append(str(msg))
        app._handle_command("/pause")
        assert app.paused is True
        assert "paused" in said[-1]
        app._handle_command("/pause")
        assert app.paused is False
        assert said[-1] == "[resumed]"

    @pytest.mark.asyncio
    async def test_the_button_flips_with_the_state(self):
        app = _app()
        app._system = lambda *a, **k: None
        async with app.run_test(size=(100, 20)) as pilot:
            btn = app.query_one(PauseButton)
            assert "pause" in str(btn.content)
            btn.on_click()                     # the mouse door
            await pilot.pause()
            assert app.paused is True
            assert "resume" in str(btn.content) and btn.has_class("paused")
            app.set_paused(False)
            await pilot.pause()
            assert "pause" in str(btn.content) and not btn.has_class("paused")

    def test_set_paused_reports_whether_it_changed(self):
        app = _app()
        app._system = lambda *a, **k: None
        assert app.set_paused(True) is True
        assert app.set_paused(True) is False
        assert app.set_paused(False) is True
