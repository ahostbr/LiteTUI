"""The first-boot engine picker: once, only when there is a real choice.

Four detection states × the chosen/dismissed axis. Esc is NOT an answer —
backend_chosen stays False and the question returns. An env-pinned backend
means there is nothing to ask (the suite itself relies on that: conftest
pins LITETUI_BACKEND so no OTHER test meets this modal).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import app as app_mod
import llm_backend
import paths
import plugins.model_switch as ms
from picker import PickerScreen
from settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-firstboot-"))


def _app():
    a = app_mod.LiteTUI()
    a.settings = Settings()
    a._connect = lambda: None
    a._system = lambda *x, **k: None
    return a


def _detect(monkeypatch, *, lms: bool, llama: bool):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    monkeypatch.setattr(ms.shutil, "which", lambda n: "lms" if lms else None)
    monkeypatch.setattr(llm_backend, "llama_available", lambda: llama)
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "cuda:test")


@pytest.mark.asyncio
async def test_both_present_shows_the_picker_once(monkeypatch):
    _detect(monkeypatch, lms=True, llama=True)
    a = _app()
    async with a.run_test() as pilot:
        ms._activate(a)
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen), "both engines and no question"
        a.screen.dismiss("llamacpp")
        await pilot.pause()
    assert a.settings.backend == "llamacpp"
    assert a.settings.backend_chosen is True
    assert a.backend.name == "llamacpp"


@pytest.mark.asyncio
async def test_escape_is_not_an_answer(monkeypatch):
    _detect(monkeypatch, lms=True, llama=True)
    a = _app()
    async with a.run_test() as pilot:
        ms._activate(a)
        await pilot.pause()
        a.screen.dismiss(None)   # Esc
        await pilot.pause()
    assert a.settings.backend_chosen is False, "a dismissed question must return"


@pytest.mark.asyncio
async def test_single_engine_is_chosen_silently(monkeypatch):
    _detect(monkeypatch, lms=False, llama=True)
    a = _app()
    async with a.run_test() as pilot:
        ms._activate(a)
        await pilot.pause()
        assert not isinstance(a.screen, PickerScreen)
    assert a.settings.backend == "llamacpp"
    assert a.settings.backend_chosen is True


@pytest.mark.asyncio
async def test_neither_engine_asks_nothing_and_keeps_asking_rights(monkeypatch):
    _detect(monkeypatch, lms=False, llama=False)
    a = _app()
    async with a.run_test() as pilot:
        ms._activate(a)
        await pilot.pause()
        assert not isinstance(a.screen, PickerScreen)
    assert a.settings.backend_chosen is False, "when one appears later, ask then"


@pytest.mark.asyncio
async def test_already_chosen_never_asks_again(monkeypatch):
    _detect(monkeypatch, lms=True, llama=True)
    a = _app()
    a.settings.backend_chosen = True
    async with a.run_test() as pilot:
        ms._activate(a)
        await pilot.pause()
        assert not isinstance(a.screen, PickerScreen)


@pytest.mark.asyncio
async def test_env_pin_means_nothing_to_ask(monkeypatch):
    _detect(monkeypatch, lms=True, llama=True)
    monkeypatch.setenv("LITETUI_BACKEND", "lmstudio")
    a = _app()
    async with a.run_test() as pilot:
        ms._activate(a)
        await pilot.pause()
        assert not isinstance(a.screen, PickerScreen), (
            "the environment already chose; a question would be a lie"
        )
