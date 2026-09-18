"""ModelConfigScreen — the per-model Load/Inference panel.

What must hold: values round-trip into the settings dicts with unset stored
as ABSENT (not zero); unknown keys already in the dicts survive a round-trip
through a screen that does not render them (forward compatibility — silent
data loss is the one forbidden outcome); LM Studio greys what its SDK cannot
drive; an invalid JSON schema is refused with the dicts untouched; a preset
saves and applies across models.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from textual.widgets import Input, Select

from litetui import app as app_mod
from litetui import llm_backend
from litetui import paths
from litetui.plugins.model_switch import ModelConfigScreen
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-modelscreen-"))

ALL_FLAGS = frozenset(llm_backend.FLAG_FOR.values()) | {"jinja"}


class _StubBackend:
    name = "llamacpp"

    def __init__(self):
        self.applied = []

    def base_url(self):
        return "http://localhost:7470/v1"

    def host(self):
        return "http://localhost:7470"

    async def apply_load_settings(self, key, cfg, *, notice=None):
        self.applied.append((key, dict(cfg)))

    def request_overrides(self, key):
        return {}


def _app(monkeypatch, backend_name="llamacpp"):
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: ALL_FLAGS)
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "cuda:test")
    a = app_mod.LiteTUI()
    a.settings = Settings()
    a._connect = lambda: None
    a.system_message = a._system = lambda *x, **k: None
    b = _StubBackend()
    b.name = backend_name
    a.backend = b
    return a


@pytest.mark.asyncio
async def test_load_values_round_trip_absent_not_zero(monkeypatch):
    a = _app(monkeypatch)
    a.settings.llama_load_settings = {"m": {"ctx": 2048}}
    async with a.run_test() as pilot:
        a.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        s = a.screen
        s.query_one("#ld-ctx", Input).value = "120064"
        s.query_one("#ld-ngl", Input).value = "65"
        s.query_one("#ld-cache_k", Select).value = "q8_0"
        s.query_one("#ld-mmap", Select).value = "false"
        s.action_apply()
        await pilot.pause()
    got = a.settings.llama_load_settings["m"]
    assert got == {"ctx": 120064, "ngl": 65, "cache_k": "q8_0", "mmap": False}
    assert "threads" not in got, "an untouched field must be stored as ABSENT"
    # And the backend was asked to apply the changed load config.
    assert a.backend.applied and a.backend.applied[0][0] == "m"


@pytest.mark.asyncio
async def test_unknown_keys_survive_the_round_trip(monkeypatch):
    a = _app(monkeypatch)
    a.settings.llama_load_settings = {"m": {"ctx": 4096, "future_knob": "kept"}}
    async with a.run_test() as pilot:
        a.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        a.screen.query_one("#ld-ngl", Input).value = "10"
        a.screen.action_apply()
        await pilot.pause()
    got = a.settings.llama_load_settings["m"]
    assert got["future_knob"] == "kept", (
        "a newer LiteTUI's setting must survive an older screen — silent "
        "data loss is the forbidden outcome"
    )


@pytest.mark.asyncio
async def test_lmstudio_greys_what_the_sdk_cannot_drive(monkeypatch):
    a = _app(monkeypatch, backend_name="lmstudio")
    async with a.run_test() as pilot:
        a.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        s = a.screen
        assert not s.query_one("#ld-ctx", Input).disabled, "ctx IS scriptable via the SDK"
        assert s.query_one("#ld-ngl", Input).disabled
        assert s.query_one("#ld-cache_k", Select).disabled


@pytest.mark.asyncio
async def test_invalid_schema_is_refused_with_dicts_untouched(monkeypatch):
    a = _app(monkeypatch)
    told = []
    a.system_message = a._system = lambda msg, *x, **k: told.append(str(msg))
    async with a.run_test() as pilot:
        a.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        a.screen.query_one("#mc-json-schema", Input).value = "{not json"
        a.screen.action_apply()
        await pilot.pause()
    assert a.settings.model_infer_overrides == {}, "a refused apply must change nothing"
    assert any("Not applied" in t for t in told)


@pytest.mark.asyncio
async def test_inference_overrides_and_preset_apply_across_models(monkeypatch):
    a = _app(monkeypatch)
    async with a.run_test() as pilot:
        a.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        s = a.screen
        s.query_one("#inf-temperature", Input).value = "0.4"
        s.query_one("#inf-top_k", Input).value = "20"
        s.query_one("#mc-preset-name", Input).value = "tiny-fast"
        s.action_apply()
        await pilot.pause()
        assert a.settings.model_infer_overrides["m"] == {"temperature": 0.4, "top_k": 20}
        assert "tiny-fast" in a.settings.llama_presets

        a.push_screen(ModelConfigScreen("other"))
        await pilot.pause()
        s2 = a.screen
        s2.query_one("#mc-preset-apply", Select).value = "tiny-fast"
        s2.action_apply()
        await pilot.pause()
    assert a.settings.model_infer_overrides["other"]["temperature"] == 0.4


@pytest.mark.asyncio
async def test_unchanged_load_config_does_not_bounce_the_model(monkeypatch):
    """Applying an inference-only edit must NOT reload the model — a reload
    evicts resident weights to change nothing (the _apply_context_length
    doctrine, screen edition)."""
    a = _app(monkeypatch)
    a.settings.llama_load_settings = {"m": {"ctx": 4096}}
    async with a.run_test() as pilot:
        a.push_screen(ModelConfigScreen("m"))
        await pilot.pause()
        a.screen.query_one("#inf-temperature", Input).value = "0.9"
        a.screen.action_apply()
        await pilot.pause()
    assert a.backend.applied == [], "load config unchanged — nothing to apply"
    assert a.settings.model_infer_overrides["m"]["temperature"] == 0.9
