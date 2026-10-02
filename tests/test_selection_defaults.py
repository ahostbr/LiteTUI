"""T0325: command selections belong to one seat, not startup defaults.

All settings/service paths are temporary via conftest. Backend construction is
real, but connect/context effects are disabled: no model or server is launched.
"""
import json
from dataclasses import replace

import pytest

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


def test_commands_change_only_the_current_seat_and_resume(monkeypatch):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    st.save(st.Settings(backend="lmstudio", default_model="alpha", temperature=.4))
    service = SettingsService(paths.data_root())
    a, b = instance(service, "a"), instance(service, "b")
    global_before = st.settings_path().read_bytes()
    b_before = convo_settings.path_for(b.convo_dir).read_bytes()

    model_switch._cmd_backend(a, "/backend", "llamacpp")
    assert st.settings_path().read_bytes() == global_before
    assert a.backend.name == "llamacpp"
    a.available_models = ["alpha", "beta"]
    model_switch._cmd_model(a, "/model", "2")
    assert a.model_id == "beta"
    assert st.settings_path().read_bytes() == global_before
    assert convo_settings.path_for(b.convo_dir).read_bytes() == b_before
    assert (b.backend.name, b.model_id) == ("lmstudio", "alpha")
    assert (a.settings.backend, a.settings.pin_default_model) == ("llamacpp", False)
    assert not saved()["backend_chosen"]

    # Actual resume adoption, without connecting/loading anything.
    resumed = instance(service, "a")
    resumed.available_models = []  # A fresh resume has no backend catalog yet.
    resumed._adopt_convo_settings(born=False)
    assert (resumed.backend.name, resumed.model_id) == ("llamacpp", "beta")
    assert st.settings_path().read_bytes() == global_before
    new = service.create_conversation("new").effective
    assert (new.backend, new.default_model, new.pin_default_model) == ("lmstudio", "alpha", False)
    fresh = st.load()
    assert (fresh.backend, fresh.default_model, fresh.pin_default_model) == ("lmstudio", "alpha", False)

    # Reselecting an existing value is still local, not a global opt-in.
    model_switch._cmd_backend(b, "/backend", "lmstudio")
    model_switch._cmd_model(b, "/model", "alpha")
    assert st.settings_path().read_bytes() == global_before
    assert convo_settings.path_for(b.convo_dir).read_bytes() == b_before
    settings_runtime.persist_or_raise(b, replace(b.settings, theme_name="ash"))
    assert (saved()["backend"], saved()["default_model"], saved()["pin_default_model"]) == ("lmstudio", "alpha", False)
    assert saved()["theme_name"] == "ash"
    assert saved()["temperature"] == .4


@pytest.mark.parametrize("arg", ["--default llamacpp", "llamacpp --default"])
def test_backend_default_requires_explicit_opt_in(monkeypatch, arg):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    st.save(st.Settings(backend="lmstudio", default_model="alpha", temperature=.4))
    service = SettingsService(paths.data_root())
    a, b = instance(service, "a"), instance(service, "b")
    b_before = convo_settings.path_for(b.convo_dir).read_bytes()
    model_switch._cmd_backend(a, "/backend", arg)
    assert a.backend.name == "llamacpp"
    assert saved()["backend"] == "llamacpp"
    assert saved()["backend_chosen"] is True
    assert saved()["default_model"] == "alpha"
    assert saved()["temperature"] == .4
    assert convo_settings.path_for(b.convo_dir).read_bytes() == b_before
    assert b.backend.name == "lmstudio"
    assert service.create_conversation("new").effective.backend == "llamacpp"


def test_default_backend_picker_and_same_backend_opt_in(monkeypatch):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    st.save(st.Settings(backend="lmstudio", default_model="alpha"))
    a = instance(SettingsService(paths.data_root()), "a")
    monkeypatch.setattr(model_switch, "backend_rows", lambda app: [("llamacpp", "test")])
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, callback, **kw: callback("llamacpp"))
    model_switch._cmd_backend(a, "/backend", "--default")
    assert saved()["backend"] == "llamacpp"
    # Explicitly choosing the already-active engine also updates defaults.
    st.save(replace(st.load(), backend="lmstudio"))
    model_switch._cmd_backend(a, "/backend", "--default llamacpp")
    assert saved()["backend"] == "llamacpp"


def test_explicit_defaults_do_not_copy_environment_or_other_conversation_settings(monkeypatch):
    st.save(st.Settings(backend="codex", default_model="old", temperature=.4))
    monkeypatch.setenv("LITETUI_BACKEND", "ninfer")
    settings_runtime.save_selection_defaults(backend="ninfer", default_model="new")
    assert (saved()["backend"], saved()["default_model"]) == ("ninfer", "new")
    assert saved()["temperature"] == .4


@pytest.mark.parametrize("pin", [False, True])
def test_model_command_and_picker_leave_global_defaults_unchanged(monkeypatch, pin):
    st.save(st.Settings(backend="lmstudio", default_model="alpha", pin_default_model=pin))
    service = SettingsService(paths.data_root())
    a = instance(service, "a")
    before = st.settings_path().read_bytes()
    model_switch._cmd_model(a, "/model", "beta")
    assert a.model_id == "beta"
    assert convo_settings.load(a.convo_dir).model == "beta"
    assert st.settings_path().read_bytes() == before
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, callback, **kw: callback("alpha"))
    model_switch._cmd_model(a, "/model", "")
    assert a.model_id == "alpha"
    assert st.settings_path().read_bytes() == before
    new = service.create_conversation("new").effective
    assert (new.default_model, new.pin_default_model) == ("alpha", pin)


def test_invalid_model_and_busy_or_unknown_backend_do_not_change_defaults():
    st.save(st.Settings(backend="lmstudio", default_model="alpha"))
    a = instance(SettingsService(paths.data_root()), "a")
    before = st.settings_path().read_bytes()
    assert not model_switch.switch_model(a, "missing")
    model_switch._cmd_backend(a, "/backend", "--default unknown")
    a._chat_running = lambda: True
    model_switch._cmd_backend(a, "/backend", "--default llamacpp")
    assert st.settings_path().read_bytes() == before


def test_ninfer_model_picker_persists_only_conversation_artifact(monkeypatch):
    st.save(st.Settings())
    service = SettingsService(paths.data_root())
    a = instance(service, "a")
    before = st.settings_path().read_bytes()
    choice = str(paths.data_root() / "chosen.ninfer")
    monkeypatch.setattr(model_switch, "_ninfer_artifact_rows", lambda app: [(choice, "chosen")])
    a._on_settings_saved = lambda candidate: settings_runtime.persist_or_raise(a, candidate)
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, callback, **kw: callback(choice))
    model_switch._pick_ninfer_artifact(a)
    assert service.snapshot("a").saved.ninfer_artifact == choice
    assert st.settings_path().read_bytes() == before


def test_commands_before_conversation_exists_do_not_fall_back_to_globals(monkeypatch):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    st.save(st.Settings(backend="lmstudio", default_model="alpha"))
    a = LiteTUI()
    a.convo_dir = None
    a._convo_settings = None
    a.connect = lambda: None
    a.update_header = lambda: None
    a.system_message = lambda *args: None
    before = st.settings_path().read_bytes()
    model_switch._cmd_backend(a, "/backend", "llamacpp")
    assert a.backend.name == "llamacpp"
    assert a.convo_dir is None
    assert st.settings_path().read_bytes() == before


def test_backend_command_does_not_publish_stale_device_preferences(monkeypatch):
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    st.save(st.Settings(backend="lmstudio", default_model="alpha"))
    service = SettingsService(paths.data_root())
    a = instance(service, "a")
    # Another seat changes an explicitly global /settings preference.
    settings_runtime.persist_or_raise(instance(service, "b"), replace(a.settings, theme_name="ash"))
    before = st.settings_path().read_bytes()
    model_switch._cmd_backend(a, "/backend", "llamacpp")
    assert st.settings_path().read_bytes() == before
    assert saved()["theme_name"] == "ash"
