"""/convos and no-arg /resume open the same picker modal - not a text list
pasted into the chat.

Regression test for the fix that made /convos and /resume share one UI:
before it, /convos printed ~30 lines of transcript text while /resume
opened a PickerScreen, so the two "list saved conversations" commands had
two different interactions. Both now dispatch to _open_convos_picker, so
they cannot drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import paths
from litetui.picker import PickerScreen
from textual.widgets import OptionList


@pytest.fixture(autouse=True)
def _store_in_tmp(tmp_path, monkeypatch):
    """Point the conversation store at a temp dir: the picker reads the
    store on command, and a test must never touch the live store.
    CONVO_DIR is a module constant, so patch IT, not ROOT."""
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")


def make_app() -> "m.LiteTUI":
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def seed_convo(uid: str = "cafe0001") -> Path:
    d = paths.CONVO_DIR / uid
    d.mkdir(parents=True)
    p = d / "convo.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "meta", "id": uid, "created": 1}) + "\n")
        f.write(json.dumps(
            {"type": "msg", "message": {"role": "user", "content": "hello"}}) + "\n")
        f.write(json.dumps(
            {"type": "msg", "message": {"role": "assistant", "content": "hi"}}) + "\n")
    return p


@pytest.mark.asyncio
async def test_convos_opens_the_picker_modal(tmp_path) -> None:
    seed_convo()
    a = make_app()
    async with a.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        base = a.screen
        a._handle_command("/convos")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen), (
            "/convos must open the picker modal, not print rows into the chat"
        )
        assert a.screen._title == "Resume a conversation"
        assert len(a.screen.query_one(OptionList).options) == 1, (
            "the seeded conversation must be one selectable row"
        )
        await pilot.press("escape")
        await pilot.pause()
        assert a.screen is base, "Esc must close the picker back to the app"


@pytest.mark.asyncio
async def test_resume_with_no_arg_opens_the_same_picker(tmp_path) -> None:
    seed_convo()
    a = make_app()
    async with a.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        a._handle_command("/resume")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen), (
            "no-arg /resume must open the same picker, not print text"
        )
        assert a.screen._title == "Resume a conversation"


@pytest.mark.asyncio
async def test_convos_with_no_savings_stays_flat(tmp_path) -> None:
    a = make_app()
    async with a.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        base = a.screen
        a._handle_command("/convos")
        await pilot.pause()
        assert a.screen is base, (
            "an empty store must not open an empty modal - say so instead"
        )


@pytest.mark.asyncio
async def test_broken_saves_badge_the_picker_title(tmp_path) -> None:
    seed_convo()
    a = make_app()
    a._persist_error = "OSError: simulated disk failure"
    async with a.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        a._handle_command("/convos")
        await pilot.pause()
        assert isinstance(a.screen, PickerScreen)
        assert "SAVING IS BROKEN" in a.screen._title, (
            "a broken save must be re-announced on the surface the "
            "user is looking at - _note_persist_error never repeats"
        )
