"""Read-only parent snapshot excludes sensitive values and carries provenance."""
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingsService
from litetui.sidecar_settings import is_sensitive, public_snapshot


def test_snapshot_scope_effective_and_revisions(tmp_path, monkeypatch):
    service = SettingsService(tmp_path)
    monkeypatch.setenv("LM_TOOL_ITERS", "77")
    result = public_snapshot(service.snapshot("abc"))
    assert result["revisions"] == {"global": "absent", "conversation": "absent"}
    assert result["fields"]["sidecar_enabled"]["scope"] == "device"
    assert result["fields"]["sidecar_enabled"]["saved"] is False
    assert result["fields"]["tool_iterations"]["scope"] == "conversation"
    assert set(result["fields"]) == set(SETTING_SPECS) - {k for k in SETTING_SPECS if is_sensitive(k)}


def test_key_token_secret_password_auth_names_are_excluded_even_without_flags(tmp_path):
    service = SettingsService(tmp_path)
    assert all(is_sensitive(name) for name in ("my_KEY", "auth_method", "secret", "access_token", "password"))
    result = public_snapshot(service.snapshot("abc"))
    assert "custom_api_key_env" not in result["fields"]
    assert not any(is_sensitive(name) for name in result["fields"])


# -- the sidecar mirrors the TUI's per-backend meaning (Ryan 2026-09-24:
#    "the sidecar is a gui representation of the settings menu") ------------

def _claude():
    from litetui.claude_backend import ClaudeBackend
    from litetui.settings import Settings

    backend = ClaudeBackend(Settings(backend="claude"))
    backend.models = {"default": {"value": "default", "resolvedModel": "claude-opus-5-5[1m]",
                                  "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]}}
    return backend


def test_claude_fields_carry_the_same_control_the_tui_settings_screen_uses(tmp_path):
    from litetui.codex_settings import control

    backend = _claude()
    result = public_snapshot(SettingsService(tmp_path).snapshot("abc"), backend=backend,
                             backends=[("claude", "Claude Agent  · OAuth signed in")],
                             models=["default", "opus[1m]"], model_id="default")
    fields = result["fields"]
    for key in fields:                      # same function, so the same answer, field by field
        want = control(backend, key)
        got = fields[key]["control"]
        assert (got is None) == (want is None), key
        if want is not None:
            assert got == {"owner": want.owner, "help": want.help, "editable": want.editable}, key
    assert fields["temperature"]["control"]["owner"] == "unsupported"
    assert fields["temperature"]["control"]["editable"] is False
    assert fields["thinking_level"]["control"]["owner"] == "native"
    assert fields["autocompact_enabled"]["control"]["owner"] == "host"
    assert "claude_executable" in fields
    assert result["backend"] == "claude"
    assert result["choices"]["backend"] == [{"value": "claude", "label": "Claude Agent  · OAuth signed in"}]
    assert result["choices"]["default_model"] == ["default", "opus[1m]"]
    values = [c["value"] for c in result["choices"]["thinking_level"]]
    assert values[:5] == ["low", "medium", "high", "xhigh", "max"]


def test_without_a_backend_the_snapshot_is_unchanged(tmp_path):
    result = public_snapshot(SettingsService(tmp_path).snapshot("abc"))
    assert "choices" not in result and "control" not in result["fields"]["temperature"]


def test_thinking_choices_translate_wire_none_to_off_for_the_saved_value():
    """PassLink: Cline reports 'none' first, and 'none' is not a ThinkingLevel."""
    from types import SimpleNamespace

    from litetui.settings_screen import thinking_choices

    cline = SimpleNamespace(name="cline", reasoning_levels=lambda m: ["none", "low", "high", "max"])
    rows = thinking_choices(cline, "glm-5.3-flash", "medium")
    assert [v for _, v in rows] == ["off", "low", "high", "max", "medium"]
    assert "not supported" in rows[-1][0]


def test_an_effort_saved_through_settings_reaches_claude_and_its_cache_gate():
    """A sidecar save runs settings_runtime.apply_saved_result (the write contract);
    that must set the level the Claude turn reads, so the SAME send-time cache
    warning fires as for /think and /modelcfg."""
    from types import SimpleNamespace

    from litetui import claude_cache, settings_runtime
    from litetui.app import LiteTUI
    from litetui.claude_turn import effort_for
    from litetui.settings import Settings

    backend = _claude()
    app = SimpleNamespace(settings=Settings(backend="claude"), _thinking_level="high", _backend=backend,
                          backend=backend, model_id="default")
    result = SimpleNamespace(persistence=[SimpleNamespace(saved=True, fields=["thinking_level"])])
    settings_runtime.apply_saved_result(app, Settings(backend="claude", thinking_level="max"), result)
    app.thinking_level = LiteTUI.thinking_level.fget(app)
    assert effort_for(app) == "max"
    clock = claude_cache.CacheClock(model="default", effort="high")
    clock.used_at, clock.read = 10**12, 100
    kind, text = claude_cache.cold_reason(clock, live=True, resuming=False, model="default",
                                          effort=effort_for(app), now=10**12 + 60)
    assert kind == "effort" and "high to max" in text


def test_a_launch_override_is_effective_and_marked_as_set_at_launch(tmp_path, monkeypatch):
    """A `--backend codex` instance shows codex, as the TUI's /settings does,
    not the saved engine. Environment overrides keep their own label."""
    from types import SimpleNamespace

    from litetui.plugins.sidecar_plugin import launch_overrides
    from litetui.settings import Settings

    app = SimpleNamespace(settings=Settings(backend="codex"), _invocation_saved_values={"backend": "lmstudio"})
    monkeypatch.setenv("LM_TOOL_ITERS", "77")
    result = public_snapshot(SettingsService(tmp_path).snapshot("abc"), launch=launch_overrides(app))
    backend = result["fields"]["backend"]
    assert (backend["effective"], backend["source"], backend["override_by"]) == ("codex", "override", "launch")
    assert backend["saved"] != "codex"
    iters = result["fields"]["tool_iterations"]
    assert (iters["effective"], iters["source"], iters["override_by"]) == (77, "override", "environment")
    assert "override_by" not in result["fields"]["sidecar_enabled"]
