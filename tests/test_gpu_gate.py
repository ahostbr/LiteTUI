"""T893 — NInfer is 5090-only. Ryan 2026-09-18 13:2x: "make sure the user never sees
anything about ninfer in both litesuite and litetui if there not running a rtx 5090 gpu ...
we must use nvidia-smi, detect if it exists on the system clean exit if not".

Every arm fakes nvidia-smi: nothing here asks the card.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from litetui import gpu_gate, llm_backend
from litetui.settings import Settings


class _Out:
    def __init__(self, text: str) -> None:
        self.stdout = text


_REAL_GATE = gpu_gate.is_rtx_5090   # the cached function; arms may monkeypatch the NAME


@pytest.fixture(autouse=True)
def _fresh_cache():
    _REAL_GATE.cache_clear()
    yield
    _REAL_GATE.cache_clear()


def test_no_nvidia_smi_is_a_clean_not_a_5090(monkeypatch):
    monkeypatch.setattr(gpu_gate, "nvidia_smi", lambda: None)
    assert gpu_gate.gpu_names() == []
    assert gpu_gate.is_rtx_5090() is False
    assert "nvidia-smi" in gpu_gate.why_not()


def test_the_name_decides_and_a_failing_query_is_a_no(monkeypatch):
    monkeypatch.setattr(gpu_gate, "nvidia_smi", lambda: "nvidia-smi")
    monkeypatch.setattr(gpu_gate.ttyguard, "run", lambda cmd, timeout=10: _Out("NVIDIA GeForce RTX 5090\n"))
    assert gpu_gate.is_rtx_5090() is True
    _REAL_GATE.cache_clear()
    monkeypatch.setattr(gpu_gate.ttyguard, "run", lambda cmd, timeout=10: _Out("NVIDIA GeForce RTX 5080\n"))
    assert gpu_gate.is_rtx_5090() is False
    assert "5080" in gpu_gate.why_not()
    _REAL_GATE.cache_clear()

    def _boom(cmd, timeout=10):
        raise OSError("driver gone")
    monkeypatch.setattr(gpu_gate.ttyguard, "run", _boom)
    assert gpu_gate.gpu_names() == []
    assert gpu_gate.is_rtx_5090() is False


def test_ninfer_is_not_a_visible_backend_off_a_5090(monkeypatch):
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    assert "ninfer" not in [n for n, _ in llm_backend.visible_backends()]
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    assert "ninfer" in [n for n, _ in llm_backend.visible_backends()]


def test_a_copied_ninfer_setting_boots_lm_studio_off_a_5090(monkeypatch):
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    s = replace(Settings(), backend="ninfer")
    backend = llm_backend._make_backend(s)
    assert backend.name == "lmstudio" and s.backend == "lmstudio"


def test_the_backend_door_refuses_ninfer_by_name_off_a_5090(monkeypatch):
    """`/backend ninfer` typed by hand, or the Engine select saved on the wrong box:
    refused BEFORE the save, with the reason. Nothing else in the sequence runs."""
    from litetui.app import LiteTUI

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    said: list[str] = []

    class _Fake:
        settings = Settings()
        def system_message(self, text: str) -> None:
            said.append(text)
    LiteTUI.apply_backend_change(_Fake(), "ninfer")
    assert said and "5090" in said[0]
    assert _Fake.settings.backend != "ninfer"


def test_the_engine_command_is_not_registered_off_a_5090(monkeypatch):
    from litetui.plugins import model_switch

    class _Ctx:
        def __init__(self) -> None:
            self.names: list[tuple[str, ...]] = []
        def command(self, names, fn, **kw) -> None:
            self.names.append(tuple(names))
        def __getattr__(self, item):           # any other registration hook: accept, ignore
            return lambda *a, **k: None

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    off = _Ctx(); model_switch._register(off)
    assert ("/engine",) not in off.names and ("/backend",) in off.names
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    on = _Ctx(); model_switch._register(on)
    assert ("/engine",) in on.names


@pytest.mark.asyncio
async def test_the_settings_screen_has_no_ninfer_tab_off_a_5090_and_still_saves(monkeypatch):
    from textual.app import App, ComposeResult
    from textual.widgets import TabPane
    from litetui.settings_screen import SettingsScreen

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    start = replace(Settings(), ninfer_max_concurrency=4)

    class Host(App):
        def compose(self) -> ComposeResult:
            return []
        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(start))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = [pane.id for pane in app.screen.query(TabPane)]
        assert "tab-ninfer" not in ids and "tab-model" in ids
        assert not app.screen.query("#f-ninfer_host")
        # The collect path: every ninfer_* field rides through untouched.
        from litetui.settings_screen import SettingsBody
        out = app.screen.query_one(SettingsBody)._collect()
        assert out.ninfer_max_concurrency == 4


@pytest.mark.asyncio
async def test_the_settings_screen_has_the_ninfer_tab_beside_model_on_a_5090(monkeypatch):
    from textual.app import App, ComposeResult
    from textual.widgets import TabPane
    from litetui.settings_screen import SettingsScreen

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)

    class Host(App):
        def compose(self) -> ComposeResult:
            return []
        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(Settings()))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = [pane.id for pane in app.screen.query(TabPane)]
        assert ids[:2] == ["tab-model", "tab-ninfer"]
        assert app.screen.query_one("#f-ninfer_max_concurrency")
