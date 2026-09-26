from __future__ import annotations

from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import ask_user_question as auq
from litetui.launch_options import LaunchOptions
from litetui.user_name_dialog import UserNameScreen

# The modal gate reads these from the live environment, and every seat LiteSuite
# spawns inherits them, so a test that expects the modal must start without them.
# The canvas-seat arm sets them back explicitly.
_HARNESS_SEAT_ENV = (
    "LITESUITE_CANVAS_AGENT",
    "LITEHARNESS_TIER",
    "LITEHARNESS_AGENT_NAME",
    "LITEHARNESS_SPAWNED_BY",
)


@pytest.fixture(autouse=True)
def _no_harness_seat_env(monkeypatch):
    for name in _HARNESS_SEAT_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Ada", ""])
async def test_first_start_name_answer_is_saved_even_when_blank(monkeypatch, name):
    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
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
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    a = app_mod.LiteTUI()
    a.settings.user_name_asked = True
    async with a.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(a.screen, UserNameScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"rpc": True},
        {"first_prompt": "continue"},
        {"convo_id": "resume-me"},
        {"launch_options": LaunchOptions(base_url="http://localhost:1234")},
        {"initial_model": "agent-model"},
        {"system_prompt": "agent preamble"},
    ],
)
async def test_automated_entry_points_never_receive_name_modal(monkeypatch, kwargs):
    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    a = app_mod.LiteTUI(**kwargs)
    a.settings.user_name_asked = False
    assert not a._should_ask_user_name()
    assert a.settings.user_name_asked is False


def test_plain_cli_main_builds_an_interactive_name_prompt(monkeypatch):
    from litetui import cli

    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.argv", ["litetui"])
    monkeypatch.setattr("litetui.image_viewer.init_image_backend", lambda: None)
    original = app_mod.LiteTUI
    observed = []

    class Launch:
        def __init__(self, **kwargs):
            app = original(**kwargs)
            app.settings.user_name_asked = False
            observed.append(app._should_ask_user_name())

        def run(self):
            pass

    monkeypatch.setattr(app_mod, "LiteTUI", Launch)
    cli.main()
    assert observed == [True]


def test_plain_cli_launch_options_still_asks_on_a_tty(monkeypatch):
    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    a = app_mod.LiteTUI(launch_options=LaunchOptions())
    a.settings.user_name_asked = False
    assert a._should_ask_user_name()


def test_canvas_seat_environment_never_receives_name_modal(monkeypatch):
    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    monkeypatch.setenv("LITESUITE_CANVAS_AGENT", "true")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    a = app_mod.LiteTUI(launch_options=LaunchOptions())
    a.settings.user_name_asked = False
    assert not a._should_ask_user_name()
    assert a.settings.user_name_asked is False


def test_non_tty_never_receives_name_modal(monkeypatch):
    monkeypatch.delenv("LITETUI_TEST_USER_NAME_ASKED", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    a = app_mod.LiteTUI()
    a.settings.user_name_asked = False
    assert not a._should_ask_user_name()


def test_shipped_prompts_and_schemas_do_not_name_ryan():
    package = Path(app_mod.__file__).resolve().parent
    offenders = []
    for directory in (package / "prompts", package / "schemas"):
        for path in directory.rglob("*"):
            if path.suffix in {".md", ".json"} and "Ryan" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(package)))
    assert offenders == []


def test_ask_user_question_results_use_neutral_language():
    submitted = auq._serialize({
        "action": "submit",
        "questions": [{"label": "Q", "question": "q", "options": [],
                       "selected": [], "note": "yes", "answered": True}],
    })
    chat = auq._serialize({
        "action": "chat",
        "questions": [{"label": "Q", "question": "q", "options": [],
                       "selected": [], "note": "", "answered": False}],
    })
    cancelled = auq._serialize({"action": "cancelled", "questions": []})
    combined = submitted + chat + cancelled
    assert "Ryan" not in combined
    assert "the user" in combined
    assert not any(word in combined.lower().split() for word in ("he", "him", "his"))
