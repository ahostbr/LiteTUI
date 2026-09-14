import pytest
from test_convo_settings import _App

from litetui import convo_settings as cs
from litetui.settings import Settings
from litetui.thinking_capabilities import set_thinking

LEVELS = ["low", "medium", "high", "xhigh", "max", "ultra"]


@pytest.mark.parametrize("level", LEVELS)
def test_selected_codex_mode_survives_actual_conversation_file_reload(tmp_path, level):
    directory = tmp_path / "conversation"
    directory.mkdir()
    app = _App(
        Settings(default_model="gpt-6-astra", thinking_level="xhigh"),
        directory,
        "codex",
    )
    app.backend.reasoning_levels = lambda _: LEVELS
    app._adopt_convo_settings(born=True)
    app.model_id = "gpt-6-astra"
    set_thinking(app, level)
    saved = cs.load(directory)
    assert saved.thinking_level == saved.reasoning_effort == level
    restored = _App(
        Settings(default_model="gpt-6-astra", thinking_level="xhigh"),
        directory,
        "codex",
    )
    restored._adopt_convo_settings(born=False)
    assert restored.thinking_level == level
    assert (
        restored.settings.model_infer_overrides["gpt-6-astra"]["reasoning_effort"]
        == level
    )


@pytest.mark.parametrize("level", ["medium", "default"])
def test_older_conversation_choice_is_not_overridden_by_another_conversations_xhigh(
    tmp_path,
    level,
):
    first, older = tmp_path / "first", tmp_path / "older"
    first.mkdir()
    older.mkdir()
    cs.save(
        first,
        cs.ConvoSettings(
            model="gpt-6-astra",
            backend="codex",
            thinking_level="xhigh",
            reasoning_effort="xhigh",
        ),
    )
    cs.save(
        older,
        cs.ConvoSettings(model="gpt-6-astra", backend="codex", thinking_level=level),
    )
    app = _App(Settings(default_model="gpt-6-astra"), first, "codex")
    app._adopt_convo_settings(born=False)
    app.convo_dir = older
    app._adopt_convo_settings(born=False)
    expected = None if level == "default" else level
    assert app.thinking_level == expected
    assert (
        app.settings.model_infer_overrides["gpt-6-astra"].get("reasoning_effort")
        == expected
    )


def test_codex_specific_saved_effort_keeps_header_and_request_override_consistent(
    tmp_path,
):
    directory = tmp_path / "conversation"
    directory.mkdir()
    cs.save(
        directory,
        cs.ConvoSettings(
            model="gpt-6-astra",
            backend="codex",
            thinking_level="xhigh",
            reasoning_effort="medium",
        ),
    )
    before = cs.path_for(directory).read_bytes()
    restored = _App(Settings(default_model="gpt-6-astra"), directory, "codex")
    restored._adopt_convo_settings(born=False)
    assert restored.thinking_level == "medium"
    assert (
        restored.settings.model_infer_overrides["gpt-6-astra"]["reasoning_effort"]
        == "medium"
    )
    assert cs.path_for(directory).read_bytes() == before
