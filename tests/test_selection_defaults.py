import json
from dataclasses import replace

from litetui import convo_settings, paths, settings_runtime
from litetui import settings as st
from litetui.app import LiteTUI
from litetui.plugins import model_switch
from litetui.settings_service import SettingsService


def saved():
    return json.loads(st.settings_path().read_text(encoding="utf-8"))


def instance(service, name):
    a = LiteTUI()
    snapshot = service.create_conversation(name)
    a.convo_dir = service._paths(name)["conversation"].parent
    a.convo_id = name
    a.settings = snapshot.effective
    a._settings_service = service
    a._convo_settings = convo_settings.load(a.convo_dir)
    a.connect = lambda: None
    a.update_header = lambda: None
    a.fetch_context_window = lambda: None
    a.apply_context_length = lambda: None
    a._probe_thinking = lambda: None
    a._rpc_emit_model_state = lambda: None
    a.system_message = lambda *args: None
    a._model_id = "alpha"
    a.available_models = ["alpha", "beta"]
    return a


def test_command_defaults_do_not_switch_other_instance_or_existing_conversation(monkeypatch):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    st.save(st.Settings(backend="lmstudio", default_model="alpha", temperature=.4))
    service = SettingsService(paths.data_root())
    a, b = instance(service, "a"), instance(service, "b")
    model_switch._switch_backend(a, "llamacpp")
    assert saved()["backend"] == "llamacpp"
    assert a.backend.name == "llamacpp"
    assert b.backend.name == "lmstudio"
    assert service.snapshot("b").effective.backend == "lmstudio"
    a.available_models = ["alpha", "beta"]
    assert model_switch.switch_model(a, "beta")
    assert saved()["default_model"] == "beta"
    assert service.snapshot("a").effective.default_model == "beta"
    assert b.model_id == "alpha"
    assert service.snapshot("b").effective.default_model == "alpha"
    # B's later unrelated save must not undo A's explicit choices.
    settings_runtime.persist_or_raise(b, replace(b.settings, theme_name="ash"))
    assert saved()["backend"] == "llamacpp"
    assert saved()["default_model"] == "beta"
    assert saved()["theme_name"] == "ash"
    assert saved()["temperature"] == .4
    new = service.create_conversation("new").effective
    assert (new.backend, new.default_model) == ("llamacpp", "beta")
    # Reselecting B's unchanged values is explicit intent, and wins globally.
    model_switch._switch_backend(b, "lmstudio")
    model_switch.switch_model(b, "alpha")
    assert (saved()["backend"], saved()["default_model"]) == ("lmstudio", "alpha")
    assert (a.backend.name, a.model_id) == ("llamacpp", "beta")


def test_explicit_defaults_do_not_copy_environment_or_other_conversation_settings(monkeypatch):
    st.save(st.Settings(backend="codex", default_model="old", temperature=.4))
    monkeypatch.setenv("LITETUI_BACKEND", "ninfer")
    settings_runtime.save_selection_defaults(backend="ninfer", default_model="new")
    assert (saved()["backend"], saved()["default_model"]) == ("ninfer", "new")
    assert saved()["temperature"] == .4


def test_invalid_model_and_busy_backend_do_not_change_defaults():
    st.save(st.Settings(backend="lmstudio", default_model="alpha"))
    a = instance(SettingsService(paths.data_root()), "a")
    before = st.settings_path().read_bytes()
    assert not model_switch.switch_model(a, "missing")
    a._chat_running = lambda: True
    model_switch._switch_backend(a, "llamacpp")
    assert st.settings_path().read_bytes() == before


def test_ninfer_model_picker_persists_artifact_default(monkeypatch):
    st.save(st.Settings())
    a = instance(SettingsService(paths.data_root()), "a")
    choice = str(paths.data_root() / "chosen.ninfer")
    monkeypatch.setattr(model_switch, "_ninfer_artifact_rows", lambda app: [(choice, "chosen")])
    def save(candidate):
        settings_runtime.persist_or_raise(a, candidate)
    a._on_settings_saved = save
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, callback, **kw: callback(choice))
    model_switch._pick_ninfer_artifact(a)
    assert saved()["ninfer_artifact"] == choice
    assert saved()["backend"] == "ninfer"
