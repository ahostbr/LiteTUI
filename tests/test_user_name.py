from __future__ import annotations

from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui.user_name_dialog import UserNameScreen


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Ada", ""])
async def test_first_start_name_answer_is_saved_even_when_blank(monkeypatch, name):
    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    a = app_mod.LiteTUI()
    a.settings.user_name = ""
    a.settings.user_name_asked = False
    saved = []
    monkeypatch.setattr(
        "litetui.settings_runtime.persist_or_raise",
        lambda app, settings: saved.append((settings.user_name, settings.user_name_asked)),
    )
    async with a.run_test() as pilot:
        await pilot.pause()
        assert isinstance(a.screen, UserNameScreen)
        a.screen.dismiss(name)
        await pilot.pause()
    assert saved == [(name, True)]


@pytest.mark.asyncio
async def test_name_question_does_not_return_after_it_was_answered(monkeypatch):
    a = app_mod.LiteTUI()
    a.settings.user_name_asked = True
    async with a.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(a.screen, UserNameScreen)


def test_shipped_prompts_and_schemas_do_not_name_ryan():
    package = Path(app_mod.__file__).resolve().parent
    offenders = []
    for directory in (package / "prompts", package / "schemas"):
        for path in directory.rglob("*"):
            if path.suffix in {".md", ".json"} and "Ryan" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(package)))
    assert offenders == []
