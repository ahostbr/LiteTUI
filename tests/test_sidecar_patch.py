"""Native edit requests reuse Textual's scoped settings adapter and revisions."""
from types import SimpleNamespace

import pytest

from litetui.settings import Settings
from litetui.settings_service import SettingsService
from litetui.sidecar_patch import apply_patch


def host(tmp_path):
    settings = Settings()
    directory = tmp_path / ".convos" / "abc"
    directory.mkdir(parents=True)
    app = SimpleNamespace(settings=settings, convo_dir=directory, _settings_service=SettingsService(tmp_path))
    return app


def payload(app, *, key="sidecar_enabled", value=True, revision=None):
    revisions = revision or app._settings_service.snapshot("abc").revisions
    scope = "device" if key == "sidecar_enabled" else "conversation"
    return {"changes": [{"key": key, "scope": scope, "value": value}],
            "expected_revisions": revisions}


def test_patch_persists_through_authoritative_service_and_applies_runtime(tmp_path):
    app = host(tmp_path)
    result = apply_patch(app, payload(app))
    assert result["saved"] is True
    assert app._settings_service.snapshot("abc").saved.sidecar_enabled is True
    assert app.settings.sidecar_enabled is True
    assert result["runtime"][0]["status"] == "applied"


def test_stale_revision_conflicts_without_overwrite(tmp_path):
    app = host(tmp_path)
    stale = payload(app)
    assert apply_patch(app, stale)["saved"] is True
    other = payload(app, value=False)
    other["expected_revisions"] = stale["expected_revisions"]
    conflict = apply_patch(app, other)
    assert conflict["saved"] is False
    assert conflict["conflict"]
    assert app._settings_service.snapshot("abc").saved.sidecar_enabled is True


def test_secret_and_mismatched_scope_rejected_before_write(tmp_path):
    app = host(tmp_path)
    with pytest.raises(ValueError, match="not editable"):
        apply_patch(app, payload(app, key="custom_api_key_env", value="LEAK"))
    bad = payload(app)
    bad["changes"][0]["scope"] = "conversation"
    with pytest.raises(ValueError, match="scope"):
        apply_patch(app, bad)
    assert app._settings_service.snapshot("abc").revisions["global"] == "absent"


def test_a_free_tier_key_is_write_only_from_the_sidecar(tmp_path):
    """Set and clear through the one save path; the reply never carries it."""
    import json

    app = host(tmp_path)
    key = "csk_fake_do_not_leak_42"
    change = {"changes": [{"key": "cerebras_api_key", "scope": "device", "value": key}],
              "expected_revisions": app._settings_service.snapshot("abc").revisions}
    result = apply_patch(app, change)
    assert result["saved"] is True
    assert app._settings_service.snapshot("abc").saved.cerebras_api_key == key
    assert key not in json.dumps(result)
    clear = {"changes": [{"key": "cerebras_api_key", "scope": "device", "value": ""}],
             "expected_revisions": result["revisions"]}
    assert apply_patch(app, clear)["saved"] is True
    assert app._settings_service.snapshot("abc").saved.cerebras_api_key == ""
    with pytest.raises(ValueError, match="Invalid key value"):
        apply_patch(app, {"changes": [{"key": "cerebras_api_key", "scope": "device", "value": 7}],
                          "expected_revisions": app._settings_service.snapshot("abc").revisions})


def test_mixed_destination_partial_conflict_retains_success(tmp_path):
    app = host(tmp_path)
    stale = app._settings_service.snapshot("abc").revisions
    # External writer advances global while conversation revision stays absent.
    assert apply_patch(app, payload(app))["saved"]
    mixed = {"changes": [
        {"key": "tool_iterations", "scope": "conversation", "value": 77},
        {"key": "sidecar_enabled", "scope": "device", "value": False},
    ], "expected_revisions": {"global": "absent", "conversation": stale["conversation"]}}
    result = apply_patch(app, mixed)
    assert result["conflict"] and not result["saved"]
    assert app._settings_service.snapshot("abc").saved.tool_iterations != 77


def test_invalid_type_and_duplicate_fields_never_write(tmp_path):
    app = host(tmp_path)
    before = app._settings_service.snapshot("abc").revisions
    invalid = payload(app, value="true")
    with pytest.raises(ValueError, match="Invalid value"):
        apply_patch(app, invalid)
    assert app._settings_service.snapshot("abc").revisions == before
    duplicate = payload(app)
    duplicate["changes"].append(dict(duplicate["changes"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        apply_patch(app, duplicate)
    assert app._settings_service.snapshot("abc").revisions == before


def test_cross_destination_race_reports_conversation_saved_global_stale(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from litetui import settings_service as service_mod
    from litetui.settings_service import SettingChange

    app = host(tmp_path)
    service = app._settings_service
    # Materialize first; this isolates the race at the two destination locks.
    service.create_conversation("abc")
    expected = service.snapshot("abc").revisions
    global_path = service._paths("abc")["global"]
    real_lock = service_mod.coordinated_write
    raced = False

    @contextmanager
    def interleave(path):
        nonlocal raced
        if path == global_path and not raced:
            raced = True
            # Another instance edits the device setting after the conversation
            # destination has committed but before our global lock/read.
            service.save_patch("abc", [SettingChange("theme_name", "monokai", "device")],
                               service.snapshot("abc").revisions)
        with real_lock(path):
            yield

    monkeypatch.setattr(service_mod, "coordinated_write", interleave)
    result = apply_patch(app, {"changes": [
        {"key": "tool_iterations", "scope": "conversation", "value": 77},
        {"key": "sidecar_enabled", "scope": "device", "value": True},
    ], "expected_revisions": expected})
    assert [(p["scope"], p["saved"]) for p in result["persistence"]] == [
        ("conversation", True), ("device", False)]
    assert result["conflict"] and not result["saved"]
    fresh = service.snapshot("abc")
    assert fresh.saved.tool_iterations == 77
    assert fresh.saved.theme_name == "monokai"
    assert fresh.saved.sidecar_enabled is False


# -- editable parity (Ryan 2026-09-24: "Yes, make it editable (full parity)") --

def test_a_field_settings_gives_no_control_is_not_editable_from_the_sidecar(tmp_path):
    """backend_chosen and the per-model dicts have no /settings control; the
    parent refuses them even if a page sent them."""
    app = host(tmp_path)
    for key in ("backend_chosen", "model_infer_overrides"):
        with pytest.raises(ValueError, match="not editable"):
            apply_patch(app, payload(app, key=key, value=True))


def test_an_effort_change_on_a_live_claude_session_asks_first_and_cancel_saves_nothing(tmp_path, monkeypatch):
    from litetui import claude_turn

    app = host(tmp_path)
    warn = ("effort", "Changing effort from high to max. ... full input price.")
    monkeypatch.setattr(claude_turn, "effort_change_warning", lambda app_, level: warn if level == "max" else None)
    monkeypatch.setattr(claude_turn, "effort_for", lambda app_: "max")
    asked = apply_patch(app, payload(app, key="thinking_level", value="max"))
    assert asked["saved"] is False and asked["conflict"] is False
    assert asked["cache_warning"]["kind"] == "effort" and "full input price" in asked["cache_warning"]["text"]
    assert app._settings_service.snapshot("abc").saved.thinking_level != "max", "nothing saved before the answer"
    assert not hasattr(app, "_claude_cache_preapproved")
    confirmed = apply_patch(app, {**payload(app, key="thinking_level", value="max"), "confirm_cache": True})
    assert confirmed["saved"] is True
    assert app._settings_service.snapshot("abc").saved.thinking_level == "max"
    assert app._claude_cache_preapproved == ("effort", "max"), "the next send does not ask again"


def test_confirm_cache_must_be_a_bool(tmp_path):
    app = host(tmp_path)
    with pytest.raises(ValueError, match="cache confirmation"):
        apply_patch(app, {**payload(app), "confirm_cache": "yes"})



@pytest.fixture
def no_backend_env(monkeypatch):
    """conftest pins LITETUI_BACKEND for the suite; a launch flag is the only
    override these arms are about."""
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)


def launched(tmp_path, convo="abc", *, backend="codex"):
    """An instance started with `--backend <backend>`: the launch value is in
    effect, the saved preference is kept aside (app.py initial_backend)."""
    app = host(tmp_path) if convo == "abc" else _host_for(tmp_path, convo)
    app._invocation_saved_values = {"backend": app.settings.backend}
    app._cli_initial_backend = backend
    app.settings.backend = backend
    return app


def _host_for(tmp_path, convo):
    directory = tmp_path / ".convos" / convo
    directory.mkdir(parents=True)
    return SimpleNamespace(settings=Settings(), convo_dir=directory, _settings_service=SettingsService(tmp_path))


def test_a_launch_set_field_edited_in_the_sidecar_survives_reconnect(tmp_path, no_backend_env):
    """Ryan: full parity. The TUI's save retires the launch value
    (settings_runtime.retire_invocation), so the edit is what the next
    reconnect adopts; the other instance's conversation is untouched."""
    from litetui.settings_runtime import prepare_reconnect

    a = launched(tmp_path)
    b = launched(tmp_path, "xyz", backend="ninfer")
    b_saved = b._settings_service.snapshot("xyz").saved.backend
    result = apply_patch(a, {"changes": [{"key": "backend", "scope": "conversation", "value": "llamacpp"}],
                             "expected_revisions": a._settings_service.snapshot("abc").revisions})
    assert result["saved"] is True
    assert result["runtime"][0]["status"] == "pending" and result["runtime"][0]["action"] == "reconnect"
    assert a._settings_service.snapshot("abc").saved.backend == "llamacpp"
    assert "backend" not in a._invocation_saved_values and a._cli_initial_backend is None
    prepare_reconnect(a)
    assert a.settings.backend == "llamacpp"
    assert b._settings_service.snapshot("xyz").saved.backend == b_saved
    assert b._invocation_saved_values == {"backend": b_saved} and b.settings.backend == "ninfer"


def test_choosing_the_saved_value_releases_the_launch_value_without_writing(tmp_path, no_backend_env):
    from litetui.settings_runtime import prepare_reconnect

    a = launched(tmp_path)
    before = a._settings_service.snapshot("abc")
    result = apply_patch(a, {"changes": [{"key": "backend", "scope": "conversation", "value": before.saved.backend}],
                             "expected_revisions": before.revisions})
    assert result["saved"] is True and result["persistence"] == []
    assert a._settings_service.snapshot("abc").revisions == before.revisions
    assert "backend" not in a._invocation_saved_values
    prepare_reconnect(a)
    assert a.settings.backend == before.saved.backend


def test_the_launch_value_itself_is_no_change(tmp_path, no_backend_env):
    a = launched(tmp_path)
    with pytest.raises(ValueError, match="No change"):
        apply_patch(a, payload(a, key="backend", value="codex"))
    assert a._invocation_saved_values == {"backend": a._settings_service.snapshot("abc").saved.backend}
