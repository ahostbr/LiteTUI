"""T893 — NInfer is 5090-only. the user 2026-09-18 13:2x: "make sure the user never sees
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


def test_the_engine_command_never_names_ninfer_off_a_5090(monkeypatch):
    """T893 holds, restated for T0374: `/engine` also drives Strata, which has no 5090
    gate, so the command is registered everywhere — but off a 5090 none of its words
    say NInfer."""
    from litetui.plugins import model_switch

    class _Ctx:
        def __init__(self) -> None:
            self.names: list[tuple[str, ...]] = []
            self.words: dict[tuple[str, ...], str] = {}
        def command(self, names, fn, **kw) -> None:
            self.names.append(tuple(names))
            self.words[tuple(names)] = f"{kw.get('palette', '')} {kw.get('help', '')}"
        def __getattr__(self, item):           # any other registration hook: accept, ignore
            return lambda *a, **k: None

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    off = _Ctx(); model_switch._register(off)
    assert ("/engine",) in off.names and ("/backend",) in off.names
    assert "ninfer" not in off.words[("/engine",)].lower() and "Strata" in off.words[("/engine",)]
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    on = _Ctx(); model_switch._register(on)
    assert ("/engine",) in on.names and "NInfer" in on.words[("/engine",)]


# The four arms below run the code and read what it SAYS. The arm above reads only the
# palette and help text, which is how three refusals went on naming NInfer off a 5090 after
# Strata joined /engine (T0374 post-merge review). Nothing here starts or probes an engine.

class _EngineApp:
    """Only what `_cmd_engine` touches before a start."""
    def __init__(self, backend) -> None:
        self.backend = backend
        self.said: list[str] = []
    def system_message(self, msg, *a, **k) -> None:
        self.said.append(str(msg))


def test_engine_run_off_a_5090_never_says_ninfer(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from litetui.plugins import model_switch
    from litetui.strata_backend import StrataBackend

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))   # never the user's config
    monkeypatch.setattr(StrataBackend, "_health_state", staticmethod(lambda host, timeout=2.0: None))

    # On a backend that has no engine: the refusal points at Strata alone.
    other = _EngineApp(SimpleNamespace(name="lmstudio"))
    model_switch._cmd_engine(other, "/engine", "status")
    # On Strata: its real status sentence, then a verb it does not have.
    strata = _EngineApp(StrataBackend(Settings(backend="strata", strata_host="http://127.0.0.1:9")))
    model_switch._cmd_engine(strata, "/engine", "status")
    model_switch._cmd_engine(strata, "/engine", "bogus")

    assert other.said == ["/engine drives the Strata backend — /backend strata first."]
    assert strata.said[0].startswith("no Strata server at http://127.0.0.1:9.")
    assert strata.said[1] == "/engine start | stop | status"
    assert not any("ninfer" in line.lower() for line in other.said + strata.said)

    # On a 5090 the same refusal names both engines.
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    both = _EngineApp(SimpleNamespace(name="lmstudio"))
    model_switch._cmd_engine(both, "/engine", "status")
    assert "NInfer" in both.said[0] and "Strata" in both.said[0]


@pytest.mark.asyncio
async def test_the_gui_engine_refusal_never_says_ninfer_off_a_5090(monkeypatch):
    from types import SimpleNamespace
    from litetui import gui_rpc

    app = SimpleNamespace(backend=SimpleNamespace(name="lmstudio"), _chat_running=lambda: False)
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    for action in ("status", "start", "stop"):
        with pytest.raises(ValueError) as refused:
            await gui_rpc.async_dispatch(app, {"type": f"gui.engine.{action}"})
        assert str(refused.value) == "engine operations require the Strata backend"

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    with pytest.raises(ValueError) as refused:
        await gui_rpc.async_dispatch(app, {"type": "gui.engine.status"})
    assert str(refused.value) == "engine operations require the NInfer or Strata backend"


def test_a_strata_launch_refusal_names_strata_and_not_ninfer(monkeypatch):
    from litetui.launch_options import LaunchOptions

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    with pytest.raises(ValueError) as context:
        LaunchOptions(context_length=8192).overrides(Settings(), "strata", None)
    with pytest.raises(ValueError) as model_path:
        LaunchOptions(model_path="strata-iq3_xxs.json").overrides(Settings(), "strata", None)
    assert str(context.value) == "Strata context is fixed at startup; use --start-server --context-length"
    assert str(model_path.value) == "--model-path for Strata requires --start-server"
    assert "ninfer" not in f"{context.value} {model_path.value}".lower()


def test_an_ninfer_launch_refusal_still_names_ninfer_on_a_5090(monkeypatch):
    from litetui.launch_options import LaunchOptions

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    with pytest.raises(ValueError) as context:
        LaunchOptions(context_length=8192).overrides(Settings(), "ninfer", None)
    with pytest.raises(ValueError) as model_path:
        LaunchOptions(model_path="a.ninfer").overrides(Settings(), "ninfer", None)
    # Word for word what they said before Strata existed (55773b3).
    assert str(context.value) == "NInfer context is fixed at startup; use --start-server --context-length"
    assert str(model_path.value) == "--model-path for NInfer requires --start-server"


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
