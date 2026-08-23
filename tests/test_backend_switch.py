"""/backend flips the engine — and ONLY the engine.

The conversation is the app's, not the model's (seat_guard's founding
observation) — so an engine switch must carry the history across untouched.
The client, the header badge, and the persisted choice all move; the
messages do not.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import llm_backend
from litetui import paths
from litetui.plugins.model_switch import _switch_backend
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-backendswitch-"))


def _app():
    a = app_mod.LiteTUI()
    a.settings = Settings()
    a._connect = lambda: None
    a._system = lambda *x, **k: None
    a._persist = lambda *x, **k: None
    return a


@pytest.mark.asyncio
async def test_switch_keeps_the_conversation(monkeypatch):
    a = _app()
    async with a.run_test() as pilot:
        a.conversation.extend([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        before = list(a.conversation)
        _switch_backend(a, "llamacpp")
        await pilot.pause()
        assert a.conversation == before, "an engine switch must not touch history"
        assert a.backend.name == "llamacpp"
        assert a.settings.backend == "llamacpp"


@pytest.mark.asyncio
async def test_switch_updates_header_badge(monkeypatch):
    a = _app()
    async with a.run_test() as pilot:
        _switch_backend(a, "llamacpp")
        await pilot.pause()
        a._update_header()
        assert "llama.cpp" in a.sub_title
        _switch_backend(a, "lmstudio")
        await pilot.pause()
        a._update_header()
        assert "LM Studio" in a.sub_title


@pytest.mark.asyncio
async def test_switch_to_same_backend_is_a_noop(monkeypatch):
    a = _app()
    async with a.run_test() as pilot:
        prior = a.backend
        _switch_backend(a, "lmstudio")
        await pilot.pause()
        assert a.backend is prior, "same-engine 'switch' must not rebuild anything"


@pytest.mark.asyncio
async def test_switch_resets_model_selection(monkeypatch):
    """The two engines hold different model lists; carrying a model id across
    would point requests at a model the new engine may not have."""
    a = _app()
    async with a.run_test() as pilot:
        a.model_id = "some-lms-model"
        _switch_backend(a, "llamacpp")
        await pilot.pause()
        assert a.model_id == ""


def test_base_url_follows_the_backend():
    s = Settings()
    s.lm_host = "http://localhost:1234"
    s.llama_host = "http://localhost:7470"
    s.backend = "lmstudio"
    assert llm_backend.make_backend(s).base_url() == "http://localhost:1234/v1"
    s.backend = "llamacpp"
    assert llm_backend.make_backend(s).base_url() == "http://localhost:7470/v1"
