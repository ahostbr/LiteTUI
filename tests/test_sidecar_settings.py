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
