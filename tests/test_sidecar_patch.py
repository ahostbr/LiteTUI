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
