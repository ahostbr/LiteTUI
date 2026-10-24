"""/model asks for a context length, remembers it per model and makes it the backend's own setting.

Ryan 2026-10-24: "anytime you use slash model to pick a model, there's supposed to be a dialogue that asks you
to directly pitch the context you want for any model that you're loading, at the time on all BACKENDS ... it
per model selection override and it must effect that backends main settings."
"""
import asyncio
from types import SimpleNamespace

import pytest

from litetui import context_choice as cc
from litetui.plugins import model_switch
from litetui.settings import Settings


# -- the pure parts ----------------------------------------------------------

@pytest.mark.parametrize("text,value", [("100000", 100000), (" 131,072 ", 131072), ("1_000_000", 1000000)])
def test_parse_accepts_plain_numbers(text, value):
    assert cc.parse_context(text) == value


@pytest.mark.parametrize("text", ["", "abc", "100k", "-5", "12.5", "100", "99999999999"])
def test_parse_refuses_everything_else(text):
    with pytest.raises(ValueError):
        cc.parse_context(text)


def test_every_backend_with_a_context_has_one_real_settings_field():
    fields = set(Settings.__dataclass_fields__)
    assert set(cc.MAIN_KEY.values()) <= fields
    assert {"lmstudio", "llamacpp", "ninfer", "strata", "xolotl", "custom"} == set(cc.MAIN_KEY)
    for hosted in ("codex", "claude", "cline", "free"):
        assert cc.main_key(hosted) is None


def test_suggestion_prefers_the_models_own_last_choice_then_the_backends_setting():
    s = Settings(strata_max_context=150000)
    assert cc.suggestion(s, "strata", "iq3_s") == 150000
    s = Settings(strata_max_context=150000, model_context_overrides={"strata:iq3_s": 90000})
    assert cc.suggestion(s, "strata", "iq3_s") == 90000
    assert cc.suggestion(s, "strata", "other") == 150000          # another model: the backend's setting
    assert cc.suggestion(Settings(), "strata", "iq3_s") is None
    assert cc.suggestion(Settings(), "codex", "gpt") is None


def test_the_answer_is_remembered_per_model_and_becomes_the_backends_main_setting():
    s = cc.settings_after(Settings(), "strata", "iq3_s", 100000)
    assert s.strata_max_context == 100000
    assert s.model_context_overrides == {"strata:iq3_s": 100000}
    s = cc.settings_after(s, "strata", "uncensored", 120000)       # a second model keeps the first's memory
    assert s.strata_max_context == 120000
    assert s.model_context_overrides == {"strata:iq3_s": 100000, "strata:uncensored": 120000}
    assert cc.settings_after(Settings(), "codex", "gpt", 5000) == Settings()


@pytest.mark.parametrize("backend", ["codex", "claude", "cline", "free"])
def test_hosted_backends_and_headless_sessions_are_not_asked(backend):
    got = []
    cc.ask(SimpleNamespace(settings=Settings(), _rpc=False), backend, "m", got.append)
    cc.ask(SimpleNamespace(settings=Settings(), _rpc=True), "strata", "m", got.append)
    assert got == [None, None]


# -- the dialog itself -------------------------------------------------------

def _drive(owned_app, backend, settings, body):
    async def main():
        app = owned_app()
        if settings is not None:
            app.settings = settings
        got = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            cc.ask(app, backend, "iq3_s", got.append)
            await pilot.pause()
            await body(app, pilot, got)
    asyncio.run(main())


@pytest.fixture
def owned_app():
    from _owned_seat import owned_app as make
    return make


def test_typing_a_number_and_pressing_set_answers_with_it(owned_app):
    async def body(app, pilot, got):
        box = app.screen.query_one("#context-input")
        assert box.value == "150000"                                  # pre-filled from the backend's setting
        box.value = "100,000"
        await pilot.click("#context-set")
        await pilot.pause()
        assert got == [100000]
    _drive(owned_app, "strata", Settings(strata_max_context=150000), body)


def test_a_bad_number_keeps_the_dialog_open_and_says_why(owned_app):
    async def body(app, pilot, got):
        app.screen.query_one("#context-input").value = "lots"
        await pilot.click("#context-set")
        await pilot.pause()
        assert got == []
        assert "number of tokens" in str(app.screen.query_one("#context-error").render())
    _drive(owned_app, "ninfer", None, body)


def test_keep_current_answers_none(owned_app):
    async def body(app, pilot, got):
        await pilot.click("#context-keep")
        await pilot.pause()
        assert got == [None]
    _drive(owned_app, "lmstudio", None, body)


def test_escape_answers_none(owned_app):
    async def body(app, pilot, got):
        await pilot.press("escape")
        await pilot.pause()
        assert got == [None]
    _drive(owned_app, "lmstudio", None, body)


def test_the_dialog_remembers_the_models_last_choice(owned_app):
    async def body(app, pilot, got):
        assert app.screen.query_one("#context-input").value == "90000"
        hint = str(app.screen.query_one("#context-hint").render())
        assert "Last used for this model: 90,000" in hint
        assert "next /engine start" in hint
    _drive(owned_app, "strata", Settings(strata_max_context=150000,
                                         model_context_overrides={"strata:iq3_s": 90000}), body)


# -- the wiring in /model ----------------------------------------------------

def _local_app(model_id="a"):
    seen = {"apply": [], "set_settings": []}
    backend = SimpleNamespace(name="lmstudio", set_settings=seen["set_settings"].append)
    app = SimpleNamespace(settings=Settings(default_context_length=8192), backend=backend, convo_dir=None,
                          available_models=["a", "b"], model_id=model_id, _rpc=False,
                          system_message=lambda *a, **k: None,
                          apply_context_length=lambda force=False: seen["apply"].append(force))
    return app, seen


def test_picking_another_model_asks_remembers_and_updates_the_main_setting(monkeypatch):
    app, seen = _local_app("a")
    switched = []
    monkeypatch.setattr(model_switch, "switch_model", lambda a, t: switched.append(t) or True)
    monkeypatch.setattr(cc, "ask", lambda a, backend, key, then, **k: then(100000))
    model_switch._choose_model(app, "b")
    assert switched == ["b"]
    assert app.settings.default_context_length == 100000
    assert app.settings.model_context_overrides == {"lmstudio:b": 100000}
    assert seen["set_settings"] == [app.settings]
    assert seen["apply"] == []                       # switch_model itself applies; no second load


def test_repicking_the_active_model_with_a_new_size_applies_it(monkeypatch):
    app, seen = _local_app("a")
    monkeypatch.setattr(model_switch, "switch_model", lambda a, t: True)
    monkeypatch.setattr(cc, "ask", lambda a, backend, key, then, **k: then(65536))
    model_switch._choose_model(app, "a")
    assert app.settings.default_context_length == 65536
    assert seen["apply"] == [True]                   # explicit, forced reload at the new window


def test_declining_changes_no_context_setting_but_the_switch_still_happens(monkeypatch):
    app, seen = _local_app("a")
    switched = []
    monkeypatch.setattr(model_switch, "switch_model", lambda a, t: switched.append(t) or True)
    monkeypatch.setattr(cc, "ask", lambda a, backend, key, then, **k: then(None))
    before = app.settings
    model_switch._choose_model(app, "b")
    assert switched == ["b"] and app.settings is before and seen["apply"] == []


def test_typed_model_commands_go_through_the_same_door(monkeypatch):
    app, _ = _local_app("a")
    chosen = []
    monkeypatch.setattr(model_switch, "_choose_model", lambda a, t: chosen.append(t))
    said = []
    app.system_message = said.append
    model_switch._cmd_model(app, "/model", "2")
    model_switch._cmd_model(app, "/model", "a")
    model_switch._cmd_model(app, "/model", "nope")
    model_switch._cmd_model(app, "/model", "9")
    assert chosen == ["b", "a"]
    assert said == ["Model not found: nope", "Invalid number. Use 1-2"]


def test_the_ninfer_artifact_pick_carries_the_context(monkeypatch, tmp_path):
    from litetui import ninfer_engine
    art = tmp_path / "qwen3_8_27b_nvfp4.ninfer"
    art.write_bytes(b"x")
    monkeypatch.setattr(ninfer_engine, "list_ninfer_artifacts", lambda: [art])
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, cb, current=None, **k: cb(str(art)))
    asked = []
    monkeypatch.setattr(cc, "ask", lambda a, backend, key, then, **k: (asked.append((backend, key)), then(100000)))
    said = []
    app = SimpleNamespace(settings=Settings(), convo_dir=None, system_message=said.append,
                          backend=SimpleNamespace(name="ninfer"))
    model_switch._pick_ninfer_artifact(app)
    assert asked == [("ninfer", "qwen3_8_27b_nvfp4")]
    assert app.settings.ninfer_artifact == str(art)
    assert app.settings.ninfer_max_context == 100000
    assert app.settings.model_context_overrides == {"ninfer:qwen3_8_27b_nvfp4": 100000}
    assert "at 100,000 tokens of context" in said[-1]


def test_the_strata_config_pick_carries_the_context(monkeypatch, tmp_path):
    cfg = tmp_path / "strata-uncensored_iq3_xxs.json"
    cfg.write_text("{}")
    engine = SimpleNamespace(list_configs=lambda s: [cfg], config_context=lambda c: 150000,
                             read_config=lambda p: {}, install_root=lambda s: tmp_path)
    backend = SimpleNamespace(name="strata", label="Strata", _engine=engine, _setting_prefix="strata",
                              _install_where="x")
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, cb, current=None, **k: cb(str(cfg)))
    asked = []
    monkeypatch.setattr(cc, "ask", lambda a, b, key, then, **k: (asked.append((b, key)), then(100000)))
    said = []
    app = SimpleNamespace(settings=Settings(), convo_dir=None, system_message=said.append, backend=backend)
    model_switch._pick_server_config(app)
    assert asked == [("strata", "uncensored_iq3_xxs")]
    assert app.settings.strata_config == str(cfg) and app.settings.strata_max_context == 100000
    assert app.settings.model_context_overrides == {"strata:uncensored_iq3_xxs": 100000}
    assert "at 100,000 tokens of context" in said[-1]
